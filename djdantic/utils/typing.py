from typing import Type, TYPE_CHECKING, TypeVar, Generic
from decimal import Decimal
from uuid import UUID
from datetime import date, datetime, timedelta, time
from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import ManyToManyRelatedManager


T = TypeVar('T')
M = TypeVar('M', bound=models.Model)


FIELD_TYPE = {
    models.AutoField: int,
    models.BigAutoField: int,
    models.BigIntegerField: int,
    models.BinaryField: bytes,
    models.BooleanField: bool,
    models.CharField: str,
    models.DateField: date,
    models.DateTimeField: datetime,
    models.DecimalField: Decimal,
    models.DurationField: timedelta,
    models.EmailField: str,
    models.FilePathField: str,
    models.FloatField: float,
    models.GenericIPAddressField: str,
    models.IPAddressField: str,
    models.IntegerField: int,
    models.PositiveBigIntegerField: int,
    models.PositiveIntegerField: int,
    models.PositiveSmallIntegerField: int,
    models.SlugField: str,
    models.SmallAutoField: int,
    models.SmallIntegerField: int,
    models.TextField: str,
    models.TimeField: time,
    models.URLField: str,
    models.UUIDField: UUID,
}


def with_typehint(baseclass: Type[T]) -> Type[T]:
    """
    Useful function to make mixins with baseclass typehint

    ```
    class ReadonlyMixin(with_typehint(BaseAdmin))):
        ...
    ```
    """
    if TYPE_CHECKING:
        return baseclass

    return object  # noqa


if not TYPE_CHECKING:
    class ManyToManyRelatedManager(Generic[M]):
        """
        Pydantic Compatible Generic Type for declaring a ManyToManyField
        """
        @classmethod
        def __get_validators__(cls):
            yield lambda v: v


def get_field_type(field: models.Field):
    if isinstance(field, models.ForeignKey):
        field.model.__annotations__.get(field.name, field.related_model)

    return field.model.__annotations__.get(field.name, FIELD_TYPE.get(type(field)))
