from typing import Optional, Mapping, TypeVar, Union
from enum import Enum
from django.db.models import Model as DjangoModel, Q, Manager
from pydantic import BaseModel as PydanticBaseModel, validate_arguments
from pydantic.fields import UndefinedType
from .utils.pydantic_django import transfer_from_orm


TDjangoModel = TypeVar('TDjangoModel', bound=DjangoModel)


class ModelKind(Enum):
    BASE = 'BASE'
    REQUEST = 'REQUEST'
    RESPONSE = 'RESPONSE'


class BaseModelValidateConfig:
    arbitrary_types_allowed = True


class BaseModel(PydanticBaseModel):
    _kind: Optional[ModelKind]
    _orm_model: Optional[TDjangoModel]
    _is_toplevel: bool

    def __init_subclass__(cls, orm_model: Optional[Union[TDjangoModel, UndefinedType]] = None, kind: Optional[ModelKind] = None, **kwargs) -> None:
        cls._orm_model = orm_model
        cls._kind = kind
        cls._is_toplevel = cls.__qualname__ == cls.__name__

        if cls._is_toplevel:
            cls.Config = type('Config', (), {'orm_mode': True})

        if cls._kind == ModelKind.RESPONSE:
            assert cls._orm_model, "When `kind` is given, the `orm_model` must also be set"

            cls._orm_model._schema_response = cls
            getattr(cls._orm_model, '_schemas_set', lambda: None)()

        return super().__init_subclass__(**kwargs)

    @classmethod
    def from_orm(cls, obj: TDjangoModel, filter_submodel: Optional[Mapping[Manager, Q]] = None):
        if not cls._is_toplevel:
            raise TypeError("from_orm can not be invoked on submodels")

        return transfer_from_orm(cls, obj, filter_submodel=filter_submodel)
