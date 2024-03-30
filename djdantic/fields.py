from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, Union

from django.db import models
from django.db.models.fields import Field as DjangoField
from pydantic.fields import FieldInfo, Undefined, UndefinedType
from pydantic.typing import NoArgAnyCallable

if TYPE_CHECKING:
    from pydantic.typing import AbstractSetIntStr, MappingIntStrAny
    from typing_extensions import Self


class ORMFieldInfo(FieldInfo):
    __slots__ = FieldInfo.__slots__ + (
        'orm_field',
        'orm_method',
        'scopes',
        'is_critical',
        'sync_matching',
        'is_sync_matching_field',
    )

    def __init__(self, default: Any = Undefined, **kwargs: Any) -> None:
        self.orm_field: Optional[Union[DjangoField, UndefinedType]] = kwargs.pop('orm_field', None)
        self.orm_method: Optional[Union[Callable[['Self'], Any], Callable[['Self', Any], None]]] = kwargs.pop(
            'orm_method', None
        )
        self.scopes: Optional[List[str]] = kwargs.pop('scopes', None)
        self.is_critical: bool = kwargs.pop('is_critical', False)
        self.sync_matching: Optional[List[Tuple[str, DjangoField]]] = kwargs.pop('sync_matching', None)
        self.is_sync_matching_field: bool = kwargs.pop('is_sync_matching_field', False)

        super().__init__(default, **kwargs)


def Field(
    default: Any = Undefined,
    *,
    default_factory: Optional[NoArgAnyCallable] = None,
    alias: str = None,
    title: str = None,
    description: str = None,
    exclude: Union['AbstractSetIntStr', 'MappingIntStrAny', Any] = None,
    include: Union['AbstractSetIntStr', 'MappingIntStrAny', Any] = None,
    const: bool = None,
    gt: float = None,
    ge: float = None,
    lt: float = None,
    le: float = None,
    multiple_of: float = None,
    max_digits: int = None,
    decimal_places: int = None,
    min_items: int = None,
    max_items: int = None,
    unique_items: bool = None,
    min_length: int = None,
    max_length: int = None,
    allow_mutation: bool = True,
    regex: str = None,
    discriminator: str = None,
    repr: bool = True,
    orm_field: Optional[Union[DjangoField, UndefinedType]] = None,
    orm_method: Optional[Union[Callable[['Self'], Any], Callable[['Self', Any], None]]] = None,
    scopes: Optional[List[str]] = None,
    is_critical: bool = False,
    sync_matching: Optional[List[Tuple[str, DjangoField]]] = None,
    is_sync_matching_field: bool = False,
    **extra: Any,
) -> Any:
    if orm_field and isinstance(orm_field, models.CharField):
        if not max_length:
            max_length = orm_field.max_length

    field_info = ORMFieldInfo(
        default,
        default_factory=default_factory,
        alias=alias,
        title=title,
        description=description,
        exclude=exclude,
        include=include,
        const=const,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        multiple_of=multiple_of,
        max_digits=max_digits,
        decimal_places=decimal_places,
        min_items=min_items,
        max_items=max_items,
        unique_items=unique_items,
        min_length=min_length,
        max_length=max_length,
        allow_mutation=allow_mutation,
        regex=regex,
        discriminator=discriminator,
        repr=repr,
        orm_field=orm_field,
        orm_method=orm_method,
        scopes=scopes,
        is_critical=is_critical,
        sync_matching=sync_matching,
        is_sync_matching_field=is_sync_matching_field,
        **extra,
    )
    field_info._validate()
    return field_info
