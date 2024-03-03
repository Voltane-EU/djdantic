import json
from contextvars import ContextVar
from decimal import Decimal
from typing import Coroutine, List, Mapping, Optional, Type, Union

from async_tools import is_async, sync_to_async
from django.db import models
from django.db.models.fields.related_descriptors import ManyToManyDescriptor, ReverseManyToOneDescriptor
from django.db.models.manager import Manager
from django.db.models.query_utils import DeferredAttribute
from django.utils.functional import cached_property
from pydantic import BaseModel, parse_obj_as
from pydantic.fields import SHAPE_LIST, SHAPE_SINGLETON, ModelField, Undefined
from pydantic.types import ConstrainedStr
from pydantic.typing import get_origin, is_union
from sentry_tools.decorators import instrument_span
from sentry_tools.span import set_data, set_tag

from ... import context
from ...exceptions import AccessError
from ...fields import ORMFieldInfo
from ...schemas import AccessScope

transfer_current_obj: ContextVar[models.Model] = ContextVar('transfer_current_obj')


class Break(Exception):
    """
    Internal Exception used to break lookping through all object attributes and instead use the given value
    """


@instrument_span(
    op='transfer_from_orm',
    description=lambda pydantic_cls, django_obj, *args, **kwargs: f'{django_obj} to {pydantic_cls.__name__}',
)
def transfer_from_orm(
    pydantic_cls: Type[BaseModel],
    django_obj: models.Model,
    django_parent_obj: Optional[models.Model] = None,
    parent_fields: Optional[List[ModelField]] = None,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
) -> Union[BaseModel, Coroutine[None, None, BaseModel]]:
    """
    Transfers the field contents of django_obj to a new instance of pydantic_cls.
    For this to work it is required to have orm_field set on all of the pydantic_obj's fields, which has to point to the django model attribute.

    It also works for nested pydantic models which point to a field on the **same** django model and for related fields (m2o or m2m).

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
        return sync_to_async(_transfer_from_orm)(
            pydantic_cls=pydantic_cls,
            django_obj=django_obj,
            django_parent_obj=django_parent_obj,
            parent_fields=parent_fields,
            filter_submodel=filter_submodel,
        )

    return _transfer_from_orm(
        pydantic_cls=pydantic_cls,
        django_obj=django_obj,
        django_parent_obj=django_parent_obj,
        parent_fields=parent_fields,
        filter_submodel=filter_submodel,
    )


def _compute_value_from_orm_method(
    orm_method: callable,
    field: ModelField,
    django_obj: models.Model,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
):
    value = orm_method(django_obj)
    if value is not None and issubclass(field.type_, BaseModel) and not isinstance(value, BaseModel):
        if field.shape == SHAPE_SINGLETON:
            if isinstance(value, models.Model):
                value = _transfer_from_orm(
                    pydantic_cls=field.type_,
                    django_obj=value,
                )

            else:
                value = field.type_.parse_obj(value)

        elif field.shape == SHAPE_LIST:
            value = [
                (
                    obj
                    if isinstance(obj, BaseModel)
                    else (
                        _transfer_from_orm(
                            pydantic_cls=field.type_,
                            django_obj=obj,
                            django_parent_obj=django_obj,
                            filter_submodel=filter_submodel,
                        )
                        if isinstance(obj, models.Model)
                        else field.type_.parse_obj(obj)
                    )
                )
                for obj in value
            ]

        else:
            raise NotImplementedError

    return value


def _transfer_field_list(
    field: ModelField,
    orm_field,
    django_obj: models.Model,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
):
    sub_filter = filter_submodel and filter_submodel.get(orm_field) or models.Q()

    if isinstance(orm_field, ManyToManyDescriptor):
        relatedmanager = getattr(django_obj, orm_field.field.attname)
        related_objs = relatedmanager.through.objects.filter(
            models.Q(**{relatedmanager.source_field_name: relatedmanager.instance}) & sub_filter
        )

    elif isinstance(orm_field, ReverseManyToOneDescriptor):
        relatedmanager = getattr(django_obj, orm_field.rel.name)
        related_objs = relatedmanager.filter(sub_filter)

    elif isinstance(orm_field, DeferredAttribute) and isinstance(orm_field.field, models.JSONField):
        value = None
        try:
            value = getattr(django_obj, orm_field.field.attname)

        except AttributeError:
            raise  # attach debugger here ;)

        return parse_obj_as(field.outer_type_, value or [])

    else:
        raise NotImplementedError

    if (
        isinstance(orm_field, (ManyToManyDescriptor, ReverseManyToOneDescriptor))
        and hasattr(relatedmanager, 'through')
        and relatedmanager.through._meta.auto_created
    ):
        related_objs = [getattr(obj, relatedmanager.target_field_name) for obj in related_objs]

    return [
        _transfer_from_orm(
            pydantic_cls=field.type_,
            django_obj=rel_obj,
            django_parent_obj=django_obj,
            parent_fields=[field],
            filter_submodel=filter_submodel,
        )
        for rel_obj in related_objs
    ]


def _transfer_field_singleton(
    field: ModelField,
    orm_field,
    django_obj: models.Model,
    parent_fields: Optional[List[ModelField]] = None,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
):
    parent_fields = parent_fields or []
    is_object = issubclass(field.type_, BaseModel)
    if not orm_field and is_object:
        return _transfer_from_orm(
            pydantic_cls=field.type_,
            django_obj=django_obj,
            parent_fields=parent_fields + [field],
            filter_submodel=filter_submodel,
        )

    value = None
    is_property = isinstance(orm_field, (property, cached_property))
    is_django_field = not is_property

    try:
        if is_property:
            if isinstance(orm_field, property):
                value = orm_field.fget(django_obj)

            elif isinstance(orm_field, cached_property):
                value = orm_field.__get__(django_obj)

            else:
                raise NotImplementedError

            if isinstance(value, models.Model):
                value = value.pk

        else:
            value = (
                getattr(django_obj, orm_field.field.name) if is_object else getattr(django_obj, orm_field.field.attname)
            )

    except AttributeError:
        raise  # attach debugger here ;)

    if field.required and value is None and parent_fields:
        for field in parent_fields[::-1]:
            if field.allow_none:
                raise Break(None)

    if is_object and isinstance(value, models.Model):
        return _transfer_from_orm(
            pydantic_cls=field.type_,
            django_obj=value,
            parent_fields=parent_fields + [field],
            filter_submodel=filter_submodel,
        )

    if is_django_field and value and isinstance(orm_field.field, models.JSONField):
        if is_object:
            if isinstance(value, dict):
                value = field.type_.parse_obj(value)

            else:
                value = field.type_.parse_raw(value)

        elif issubclass(field.type_, dict):
            if isinstance(value, str):
                value = json.loads(value)

        else:
            raise NotImplementedError

    scopes = [
        AccessScope.from_str(audience)
        for audience in (
            field.field_info.scopes
            if isinstance(field.field_info, ORMFieldInfo)
            else field.field_info.extra.get('scopes')
        )
        or []
    ]
    if scopes:
        try:
            access = context.access.get()

        except LookupError:
            pass

        else:
            read_scopes = [str(scope) for scope in scopes if scope.action == 'read']
            if read_scopes:
                _value = None
                if not field.allow_none:
                    if issubclass(field.type_, str):
                        _value = '•' * (
                            field.type_.max_length if issubclass(field.type_, ConstrainedStr) else len(value)
                        )

                    elif issubclass(field.type_, (int, float, Decimal)):
                        _value = 0

                    else:
                        raise NotImplementedError

                if not access.token.has_audience(read_scopes):
                    value = _value

                else:
                    if hasattr(django_obj, 'check_access'):
                        for scope in scopes:
                            if scope.action != 'read':
                                continue

                            try:
                                django_obj.check_access(access, selector=scope.selector)

                            except AccessError:
                                value = _value

    return value


def _transfer_field(
    field: ModelField,
    django_obj: models.Model,
    parent_fields: Optional[List[ModelField]] = None,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
):
    orm_method = (
        field.field_info.orm_method
        if isinstance(field.field_info, ORMFieldInfo)
        else field.field_info.extra.get('orm_method')
    )
    if orm_method:
        return _compute_value_from_orm_method(
            orm_method=orm_method,
            field=field,
            django_obj=django_obj,
            filter_submodel=filter_submodel,
        )

    if isinstance(field.field_info, ORMFieldInfo):
        orm_field = field.field_info.orm_field
        if orm_field is Undefined:
            return ...

    else:
        orm_field = field.field_info.extra.get('orm_field')
        if 'orm_field' in field.field_info.extra and field.field_info.extra['orm_field'] is None:
            # Do not raise error when orm_field was explicitly set to None
            return ...

    if not orm_field and not (field.shape == SHAPE_SINGLETON and issubclass(field.type_, BaseModel)):
        raise AttributeError("orm_field not found on %r (parents: %r)" % (field, parent_fields))

    if field.shape == SHAPE_SINGLETON:
        return _transfer_field_singleton(
            field=field,
            orm_field=orm_field,
            django_obj=django_obj,
            filter_submodel=filter_submodel,
            parent_fields=parent_fields,
        )

    if field.shape == SHAPE_LIST:
        return _transfer_field_list(
            field=field,
            orm_field=orm_field,
            django_obj=django_obj,
            filter_submodel=filter_submodel,
        )

    raise NotImplementedError


def _transfer_from_orm(
    pydantic_cls: Type[BaseModel],
    django_obj: models.Model,
    django_parent_obj: Optional[models.Model] = None,
    parent_fields: Optional[List[ModelField]] = None,
    filter_submodel: Optional[Mapping[Manager, models.Q]] = None,
) -> Union[BaseModel, Coroutine[None, None, BaseModel]]:
    set_tag('transfer_from_orm.pydantic_cls', pydantic_cls.__name__)
    set_tag('transfer_from_orm.django_cls', django_obj.__class__.__name__)
    set_data('transfer_from_orm.django_obj', django_obj)
    set_data('transfer_from_orm.django_parent_obj', django_parent_obj)
    set_data('transfer_from_orm.filter_submodel', filter_submodel)

    transfer_current_obj.set(django_obj)

    values = {}
    field: ModelField
    if is_union(get_origin(pydantic_cls)):
        raise ValueError("cannot use union type on response model")

    for field in pydantic_cls.__fields__.values():
        try:
            value = _transfer_field(
                field=field,
                django_obj=django_obj,
                parent_fields=parent_fields,
                filter_submodel=filter_submodel,
            )

        except Break as break_:
            if field.allow_none:
                # The whole object should be None
                value = break_.args[0]

            else:
                raise

        if value is ...:
            continue

        values[field.name] = value

    return pydantic_cls.construct(**values)
