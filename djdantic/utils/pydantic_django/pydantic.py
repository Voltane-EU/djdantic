import warnings
from typing import Any, Dict, Generator, Iterable, Mapping, Optional, Tuple, Type, TypeVar, Union

from django.db import models
from django.db.models.manager import Manager
from pydantic import BaseModel, Field, validate_model
from pydantic.error_wrappers import ErrorWrapper

from ... import context
from ..pydantic import Reference, get_orm_field_attr
from .django_to_pydantic import transfer_from_orm

try:
    from fastapi.exceptions import RequestValidationError

except ImportError:
    from pydantic import ValidationError as RequestValidationError


class DjangoORMBaseModel(BaseModel):
    @classmethod
    def from_orm(cls, obj: models.Model, filter_submodel: Optional[Mapping[Manager, models.Q]] = None):
        return transfer_from_orm(cls, obj, filter_submodel=filter_submodel)

    class Config:
        orm_mode = True


def validate_object(obj: BaseModel, is_request: bool = True):
    *_, validation_error = validate_model(obj.__class__, obj.__dict__)
    if validation_error:
        if is_request:
            raise RequestValidationError(validation_error.raw_errors)

        raise validation_error


TDjangoModel = TypeVar('TDjangoModel', bound=models.Model)


def orm_object_validator(model: Type[TDjangoModel], value: Union[str, models.Q]) -> TDjangoModel:
    warnings.warn("orm_object_validator is deprecated", category=DeprecationWarning)
    if isinstance(value, str):
        value = models.Q(id=value)

    access = context.access.get()
    if access and hasattr(model, 'tenant_id'):
        value &= models.Q(tenant_id=access.tenant_id)

    from djutils.asyncio import AllowAsyncUnsafe

    with AllowAsyncUnsafe():
        try:
            return model.objects.get(value)

        except model.DoesNotExist:
            raise ValueError('reference_not_exist')


def get_sync_matching_values(
    model: BaseModel,
    django_field_names: Optional[Iterable[str]] = None,
) -> Generator[Tuple[models.Field, Any], None, None]:
    for name, field in model.__fields__.items():
        if isinstance(field, BaseModel):
            yield from get_sync_matching_values(field)

        if not django_field_names and not get_orm_field_attr(field.field_info, 'is_sync_matching_field'):
            continue

        orm_field = get_orm_field_attr(field.field_info, 'orm_field')
        if django_field_names and orm_field.field.name not in django_field_names:
            continue

        yield (orm_field, getattr(model, name))


def get_sync_matching_filter(
    model: BaseModel,
    django_model: Optional[Type[models.Model]] = None,
    field: Optional[Field] = None,
    obj_fields: Optional[Dict[str, models.Model]] = None,
) -> models.Q:
    # Legacy sync_matching handling
    if field and (
        sync_matching := get_orm_field_attr(
            field.field_info,
            'sync_matching',
        )
    ):
        if isinstance(sync_matching, list):
            matching_search = models.Q()
            pydantic_field_name: str
            match_orm_field: models.Field
            for pydantic_field_name, match_orm_field in sync_matching:
                match_value = model
                for _field in pydantic_field_name.split('.'):
                    match_value = getattr(match_value, _field)

                if isinstance(match_value, Reference):
                    match_value = match_value.id

                matching_search &= models.Q(**{match_orm_field.field.attname: match_value})

            return models.Q(**obj_fields) & matching_search

        elif isinstance(sync_matching, callable):
            raise NotImplementedError

        else:
            raise NotImplementedError

    fields = {field.field.name: value for field, value in get_sync_matching_values(model)} or (
        {'id': model.id} if getattr(model, 'id', None) else {}
    )

    if not fields:
        if django_model and len(django_model._meta.total_unique_constraints) == 1:
            fields = {
                field.field.name: value
                for field, value in get_sync_matching_values(
                    model,
                    django_field_names=django_model._meta.total_unique_constraints[0].fields,
                )
            }

        else:
            raise ValueError('no_fields_for_matching_defined')

    if not all(fields.values()):
        raise RequestValidationError(
            errors=[ErrorWrapper(ValueError('not_all_required_fields_for_matching_given'), '')],
        )

    return models.Q(**fields)
