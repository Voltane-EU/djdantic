from typing import Optional, Mapping, TypeVar, Union
from warnings import warn
from django.db.models import Model as DjangoModel, Q, Manager
from pydantic import BaseModel as PydanticBaseModel, validate_arguments
from pydantic.fields import UndefinedType
from .utils.pydantic_django import transfer_from_orm


TDjangoModel = TypeVar('TDjangoModel', bound=DjangoModel)


class BaseModelValidateConfig:
    arbitrary_types_allowed = True


class BaseModel(PydanticBaseModel):
    __orm_model: TDjangoModel
    __is_toplevel: bool

    def __init_subclass__(cls, orm_model: Optional[Union[TDjangoModel, UndefinedType]] = None, **kwargs) -> None:
        cls.__orm_model = orm_model
        cls.__is_toplevel = cls.__qualname__ == cls.__name__

        if cls.__is_toplevel:
            cls.Config = type('Config', (), {'orm_mode': True})

        return super().__init_subclass__(**kwargs)

    @classmethod
    def from_orm(cls, obj: TDjangoModel, filter_submodel: Optional[Mapping[Manager, Q]] = None):
        if not cls.__is_toplevel:
            raise TypeError("from_orm can not be invoked on submodels")

        return transfer_from_orm(cls, obj, filter_submodel=filter_submodel)
