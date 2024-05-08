import warnings
from collections import defaultdict
from enum import Enum
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple, Type

from async_tools import is_async, sync_to_async
from django.db import models
from django.db.models.fields import Field as DjangoField
from django.db.models.fields.related_descriptors import ManyToManyDescriptor, ReverseManyToOneDescriptor
from django.db.transaction import atomic
from pydantic import BaseModel, Field, SecretStr, validate_model
from pydantic.fields import SHAPE_LIST, SHAPE_SINGLETON, Undefined, UndefinedType
from sentry_tools.decorators import instrument_span
from sentry_tools.span import set_data, set_tag

from ...schemas import Access
from ..pydantic import Reference, get_orm_field_attr, is_orm_field_set
from .checks import check_field_access
from .pydantic import get_sync_matching_filter

try:
    from dirtyfields import DirtyFieldsMixin

except ImportError:

    class DirtyFieldsMixin:
        pass


try:
    from fastapi.exceptions import RequestValidationError

except ImportError:
    from pydantic import ValidationError as RequestValidationError


class TransferAction(Enum):
    CREATE = 'CREATE'
    SYNC = 'SYNC'
    NO_SUBOBJECTS = 'NO_SUBOBJECTS'


def get_subobj_many_to_many(
    val: BaseModel,
    action: TransferAction,
    related_model: Type[models.Model],
    relatedmanager: models.Manager,
    force_create: bool = False,
    allow_creation: bool = True,
):
    q_filter = models.Q()
    if getattr(val, 'id', None):
        q_filter &= models.Q(**{'pk': val.id})

    elif matching := get_sync_matching_filter(val, related_model):
        q_filter &= matching

    else:
        raise NotImplementedError

    try:
        return related_model.objects.get(q_filter)

    except related_model.DoesNotExist:
        if not allow_creation:
            raise

        return related_model()


def get_subobj_many_to_many_with_intermediate(
    val: BaseModel,
    obj_fields: Dict[str, models.Model],
    action: TransferAction,
    related_model: Type[models.Model],
    relatedmanager: models.Manager,
    force_create: bool = False,
):
    obj_manytomany_fields = {**obj_fields}
    if getattr(val, 'id', None):
        obj_manytomany_fields[relatedmanager.target_field.attname] = val.id

    else:
        raise NotImplementedError

    if force_create or action == TransferAction.CREATE:
        return related_model(**obj_manytomany_fields)

    elif action == TransferAction.SYNC:
        try:
            return related_model.objects.get(**obj_manytomany_fields)

        except related_model.DoesNotExist:
            return get_subobj_many_to_many_with_intermediate(
                val,
                obj_fields,
                action,
                related_model,
                relatedmanager,
                force_create=True,
            )

    else:
        raise NotImplementedError


def get_subobj_rev_many_to_one(
    val: BaseModel,
    obj_fields: Dict[str, models.Model],
    action: TransferAction,
    related_model: Type[models.Model],
    field: Field,
    force_create: bool = False,
):
    q_filter = models.Q()
    try:
        if getattr(val, 'id', None):
            q_filter &= models.Q(**{'pk': val.id})

        elif matching := get_sync_matching_filter(val, related_model, field, obj_fields):
            q_filter &= matching

    except ValueError:
        # its okay to have no_fields_for_matching_defined
        pass

    if action == TransferAction.SYNC and not getattr(val, 'id', None) and not q_filter:
        force_create = True

    if force_create or action == TransferAction.CREATE:
        return related_model(**obj_fields)

    elif action == TransferAction.SYNC:
        try:
            if getattr(val, 'id', None):
                return related_model.objects.get(id=val.id, **obj_fields)

            elif q_filter:
                return related_model.objects.filter(**obj_fields).get(q_filter)

            else:
                raise NotImplementedError

        except related_model.DoesNotExist:
            return get_subobj_rev_many_to_one(val, obj_fields, action, related_model, field, force_create=True)

    else:
        raise NotImplementedError


@instrument_span(
    op='transfer_to_orm',
    description=lambda pydantic_obj, django_obj, *args, **kwargs: f'{pydantic_obj} to {django_obj}',
)
def transfer_to_orm(
    pydantic_obj: BaseModel,
    django_obj: models.Model,
    *,
    action: Optional[TransferAction] = None,
    exclude_unset: bool = False,
    access: Optional[Access] = None,
    created_submodels: Optional[List[models.Model]] = None,
    _just_return_objs: bool = False,
    do_not_save_if_no_change: bool = False,
) -> Optional[Tuple[List[models.Model], List[models.Model]]]:
    """
    Transfers the field contents of pydantic_obj to django_obj.
    For this to work it is required to have orm_field set on all of the pydantic_obj's fields, which has to point to the django model attribute.

    It also works for nested pydantic models which point to a field on the **same** django model.

    Example:

    ```python
    from pydantic import BaseModel, Field
    from django.db import models

    class Address(models.Model):
        name = models.CharField(max_length=56)

    class AddressRequest(BaseModel):
        name: str = Field(orm_field=Address.name)
    ```
    """
    if is_async():
        return sync_to_async(transfer_to_orm)(
            pydantic_obj=pydantic_obj,
            django_obj=django_obj,
            action=action,
            exclude_unset=exclude_unset,
            access=access,
            created_submodels=created_submodels,
        )

    set_tag('transfer_to_orm.action', action)
    set_tag('transfer_to_orm.exclude_unset', exclude_unset)
    set_data('transfer_to_orm.access', access)
    set_data('transfer_to_orm.pydantic_obj', pydantic_obj)
    set_data('transfer_to_orm.django_obj', django_obj)

    if created_submodels:
        warnings.warn("Use transfer_to_orm with kwarg action instead of created_submodels", category=DeprecationWarning)
        action = TransferAction.CREATE

    if not action:
        warnings.warn("Use transfer_to_orm with kwarg action", category=DeprecationWarning)

    subobjects: List[models.Model] = created_submodels or []
    objects_to_delete = []
    many_to_many_objs: Dict[models.Manager, List[models.Model]] = {}

    if access:
        check_field_access(pydantic_obj, access)

    pydantic_values: Optional[dict] = pydantic_obj.dict(exclude_unset=True) if exclude_unset else None

    def populate_default(pydantic_cls: BaseModel, django_obj):
        for key, field in pydantic_cls.__fields__.items():
            orm_field = get_orm_field_attr(field.field_info, 'orm_field')
            if not orm_field and issubclass(field.type_, BaseModel):
                populate_default(field.type_, django_obj)

            else:
                if not is_orm_field_set(field.field_info):
                    continue

                if get_orm_field_attr(field.field_info, 'orm_method'):
                    # Do not raise error when orm_method is set
                    continue

                orm_field: DjangoField = get_orm_field_attr(field.field_info, 'orm_field')

                assert orm_field, "orm_field not set on %r of %r" % (field, pydantic_cls)

                setattr(
                    django_obj,
                    orm_field.field.attname,
                    (
                        field.field_info.default
                        if field.field_info.default is not Undefined and field.field_info.default is not ...
                        else None
                    ),
                )

    fields = pydantic_obj.__fields__.items()
    if exclude_unset:
        # XXX: is filtering the fields at this point correct? Tests required
        fields = [(key, field) for key, field in fields if key in pydantic_values]

    for key, field in fields:
        orm_field = get_orm_field_attr(field.field_info, 'orm_field')
        orm_method = get_orm_field_attr(field.field_info, 'orm_method')

        if (
            key == 'id'
            and ((not orm_field or isinstance(orm_field, UndefinedType)) or orm_field.field.attname == key)
            and not orm_method
        ):
            continue

        if orm_method:
            if exclude_unset and key not in pydantic_values:
                continue

            value = getattr(pydantic_obj, field.name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()

            orm_method(django_obj, value)
            continue

        if not is_orm_field_set(field.field_info) and not (
            field.shape == SHAPE_SINGLETON and issubclass(field.type_, BaseModel)
        ):
            continue

        if not orm_field and not (field.shape == SHAPE_SINGLETON and issubclass(field.type_, BaseModel)):
            raise AttributeError("orm_field not found on %r" % field)

        value = getattr(pydantic_obj, field.name)
        if field.shape == SHAPE_SINGLETON:
            if not orm_field and issubclass(field.type_, BaseModel):
                if value is None:
                    if exclude_unset and key not in pydantic_values:
                        continue

                    populate_default(field.type_, django_obj)

                elif isinstance(value, BaseModel):
                    sub_transfer = transfer_to_orm(
                        pydantic_obj=value,
                        django_obj=django_obj,
                        exclude_unset=exclude_unset,
                        access=access,
                        action=action,
                        _just_return_objs=True,
                    )
                    subobjects += sub_transfer[0]
                    objects_to_delete += sub_transfer[1]

                else:
                    raise NotImplementedError

            else:
                if exclude_unset and key not in pydantic_values:
                    continue

                if orm_field.field.is_relation and isinstance(value, models.Model):
                    value = value.pk

                if isinstance(orm_field.field, models.JSONField) and value:
                    if isinstance(value, BaseModel):
                        value = value.dict()

                    elif isinstance(value, dict):
                        pass

                    else:
                        raise NotImplementedError

                setattr(django_obj, orm_field.field.attname, value)

        elif field.shape == SHAPE_LIST:
            if value is None:
                continue

            is_direct_m2m = False
            related_model: Type[models.Model]
            relatedmanager: models.Manager

            if isinstance(orm_field, ManyToManyDescriptor):
                relatedmanager = getattr(django_obj, orm_field.field.attname)

                if hasattr(relatedmanager, 'through') and relatedmanager.through._meta.auto_created:
                    obj_fields = relatedmanager.core_filters
                    related_model = relatedmanager.model
                    get_subobj = partial(
                        get_subobj_many_to_many,
                        action=action,
                        related_model=related_model,
                        relatedmanager=relatedmanager,
                        allow_creation=action in (TransferAction.CREATE, TransferAction.SYNC),
                    )
                    is_direct_m2m = True

                else:
                    obj_fields = {relatedmanager.source_field_name: django_obj}
                    related_model = relatedmanager.through
                    get_subobj = partial(
                        get_subobj_many_to_many_with_intermediate,
                        obj_fields=obj_fields,
                        action=action,
                        related_model=related_model,
                        relatedmanager=relatedmanager,
                    )

            elif isinstance(orm_field, ReverseManyToOneDescriptor):
                relatedmanager = getattr(django_obj, orm_field.rel.name)
                related_model = relatedmanager.field.model
                obj_fields = {relatedmanager.field.name: django_obj}

                get_subobj = partial(
                    get_subobj_rev_many_to_one,
                    obj_fields=obj_fields,
                    action=action,
                    related_model=related_model,
                    field=field,
                )

            else:
                raise NotImplementedError

            existing_object_ids = set(related_model.objects.filter(**obj_fields).values_list('id', flat=True))

            many_to_many_objs[relatedmanager] = []
            val: BaseModel
            for val in value:
                sub_obj = get_subobj(val)
                existing_object_ids.discard(sub_obj.id)
                subobjects.append(sub_obj)
                sub_transfer = transfer_to_orm(
                    val,
                    sub_obj,
                    exclude_unset=exclude_unset,
                    access=access,
                    action=action,
                    _just_return_objs=True,
                )
                subobjects += sub_transfer[0]
                objects_to_delete += sub_transfer[1]

                if is_direct_m2m:
                    many_to_many_objs[relatedmanager].append(sub_obj)

            if not is_direct_m2m:
                objects_to_delete += related_model.objects.filter(id__in=list(existing_object_ids))

        else:
            raise NotImplementedError

    if subobjects and not action:
        raise AssertionError('action is not defined but subobjects exist')

    if _just_return_objs:
        return subobjects, objects_to_delete

    if (
        action in (TransferAction.CREATE, TransferAction.SYNC, TransferAction.NO_SUBOBJECTS)
        and created_submodels is None
    ):
        with atomic():
            should_save: Callable[[models.Model], bool] = lambda obj: bool(
                not do_not_save_if_no_change
                or (isinstance(obj, DirtyFieldsMixin) and obj.get_dirty_fields(check_relationship=True))
            )
            if should_save(django_obj):
                django_obj.save()

            for manager, objs in many_to_many_objs.items():
                for obj in objs:
                    if should_save(obj):
                        obj.save()

                manager.set(objs)

            if action in (TransferAction.SYNC, TransferAction.NO_SUBOBJECTS):
                for obj in objects_to_delete:
                    obj.delete()

            if action in (TransferAction.CREATE, TransferAction.SYNC):
                for obj in subobjects:
                    if should_save(obj):
                        obj.save()


async def update_orm(
    model: Type[BaseModel], orm_obj: models.Model, input: BaseModel, *, access: Optional[Access] = None
) -> BaseModel:
    """
    Apply (partial) changes given in `input` to an orm_obj and return an instance of `model` with the full data of the orm including the updated fields.
    """
    warnings.warn("Use transfer_to_orm with exclude_unset=True instead of this function", category=DeprecationWarning)

    if access:
        check_field_access(input, access)

    data = await model.from_orm(orm_obj)
    input_dict: dict = input.dict(exclude_unset=True)

    def update(model: BaseModel, input: dict):
        for key, value in input.items():
            if isinstance(value, dict):
                attr = getattr(model, key)
                if attr is None:
                    setattr(model, key, model.__fields__[key].type_.parse_obj(value))

                else:
                    update(attr, value)

            else:
                setattr(model, key, value)

    update(data, input_dict)

    values, fields_set, validation_error = validate_model(model, data.dict())
    if validation_error:
        raise RequestValidationError(validation_error.raw_errors)

    transfer_to_orm(data, orm_obj)
    return data
