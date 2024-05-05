import logging
import typing
from functools import cache
from types import FunctionType
from typing import Any, Callable, ForwardRef, Optional, Type, Union

from pydantic import BaseModel
from pydantic import Field as Field
from pydantic import create_model
from pydantic.fields import SHAPE_LIST, SHAPE_SINGLETON, FieldInfo, ModelField, Undefined
from pydantic.typing import get_origin, is_union

from ..fields import Field as ORMField
from ..fields import ORMFieldInfo

_logger = logging.getLogger(__name__)

TypingGenericAlias = type(Any)

_recreated_models = {}
_optional_models = {}
_id_added_models = {}


def _new_field_from_model_field(field: ModelField, default: Any = Undefined, required: Optional[bool] = None):
    if default is not Undefined:
        default = field.default

    if required is None and field.required and (default is Undefined or field.default is None):
        default = ...

    if isinstance(field.field_info, ORMFieldInfo):
        return ORMField(
            default,
            default_factory=field.default_factory,
            alias=field.alias,
            orm_field=field.field_info.orm_field,
            orm_method=field.field_info.orm_method,
            scopes=field.field_info.scopes,
            is_critical=field.field_info.is_critical,
            sync_matching=field.field_info.sync_matching,
            is_sync_matching_field=field.field_info.is_sync_matching_field,
            **field.field_info.extra,
        )

    return Field(
        default,
        default_factory=field.default_factory,
        alias=field.alias,
        **field.field_info.extra,
    )


class IdAddedModel(BaseModel):
    pass


def id_added_model(
    cls,
    __module__: Optional[str] = None,
    __parent__module__: Optional[str] = None,
):
    if not __module__:
        __module__ = cls.__module__
    if not __parent__module__:
        __parent__module__ = cls.__base__.__module__

    try:
        if issubclass(cls, BaseModel):
            if 'id' in cls.__fields__:
                return cls

            if cls in _id_added_models:
                return _id_added_models[cls]

            django_model = getattr(cls, '_orm_model', None)

            field: ModelField
            fields = {}
            for key, field in cls.__fields__.items():
                # TODO handle ForwardRef
                if field.shape in (SHAPE_SINGLETON, SHAPE_LIST):
                    field_type = id_added_model(
                        field.type_,
                        __module__=__module__,
                        __parent__module__=__parent__module__,
                    )

                    if field.type_ != field.outer_type_:
                        field_type = getattr(typing, field.outer_type_._name)[field_type]

                else:
                    # TODO pydantic.get_origin ??
                    field_type = field.outer_type_

                if field.allow_none:
                    field_type = Optional[field_type]

                fields[key] = (
                    field_type,
                    _new_field_from_model_field(field),
                )

            fields['id'] = (Optional[str], ORMField(orm_field=django_model.id if django_model else Undefined))

            _logger.debug("ID Added Model %s", cls)
            _id_added_models[cls] = create_model(
                f'{cls.__qualname__} [ID]',
                __base__=(cls, IdAddedModel),
                __module__=cls.__module__ if cls.__module__ != __parent__module__ else __module__,
                **fields,
            )

            return _id_added_models[cls]

    except TypeError as error:
        _logger.warning("TypeError when handling id_added_model: %s", error, exc_info=True, stack_info=True)

    return cls


class OptionalModel(BaseModel):
    pass


def optional_model(cls, __module__: Optional[str] = None, __parent__module__: Optional[str] = None, id_key: str = 'id'):
    if not __module__:
        __module__ = cls.__module__
    if not __parent__module__:
        __parent__module__ = cls.__base__.__module__

    try:
        if issubclass(cls, BaseModel):
            if cls in _optional_models:
                return _optional_models[cls]

            field: ModelField
            fields = {}
            for key, field in cls.__fields__.items():
                # TODO handle ForwardRef
                if field.shape == SHAPE_SINGLETON:
                    field_type = optional_model(
                        field.outer_type_,
                        __module__=__module__,
                        __parent__module__=__parent__module__,
                        id_key=id_key,
                    )

                else:
                    # TODO pydantic.get_origin ??
                    field_type = field.outer_type_

                default = field.default
                if key == id_key and not field.allow_none:
                    default = default or ...

                elif not field.allow_none:
                    field_type = Optional[field_type]

                elif field.required:
                    default = default or ...

                fields[key] = (field_type, _new_field_from_model_field(field, default, required=False))

            _logger.debug("Optional Model %s", cls)
            _optional_models[cls] = create_model(
                f'{cls.__qualname__} [O]',
                __base__=(cls, OptionalModel),
                __module__=cls.__module__ if cls.__module__ != __parent__module__ else __module__,
                **fields,
            )

            return _optional_models[cls]

    except TypeError as error:
        _logger.warning("TypeError when handling optional_model: %s", error, exc_info=True, stack_info=True)

    return cls


def to_optional(id_key: str = 'id'):
    def wrapped(cls: Type[BaseModel]):

        return optional_model(
            cls,
            __module__=cls.__module__,
            __parent__module__=cls.__base__.__module__,
            id_key=id_key,
        )

    return wrapped


class ReferencedModel(BaseModel):
    pass


class Reference(BaseModel):
    def __init_subclass__(cls, rel: Optional[str] = None, rel_params: Optional[Callable] = None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls._rel = getattr(cls, '_rel', rel)
        cls._rel_params = rel_params
        c = cls
        while cls._rel is None:
            if issubclass(c.__base__, Reference):
                c = c.__base__
                if not c:
                    raise AssertionError("Cannot find parent Reference with `rel` set")

                try:
                    cls._rel = c._rel
                    cls._rel_params = c._rel_params

                except AttributeError:
                    pass

            else:
                raise AssertionError


def include_reference(reference_key: str = '$rel', reference_params_key: str = '$rel_params'):
    def wrapped(cls: Type[BaseModel]):
        def model_with_rel(c: Type, __parent__: Type, __module__: str, __parent__module__: str):
            if isinstance(c, ForwardRef):
                return c, False

            if is_union(get_origin(c)):
                models = [
                    model_with_rel(m, c, __module__=__module__, __parent__module__=__parent__module__)
                    for m in c.__args__
                ]
                return Union[(m[0] for m in models)], any(m[1] for m in models)

            if issubclass(c, BaseModel):
                field: ModelField
                fields = {}
                recreate_model = False
                for key, field in c.__fields__.items():
                    if field.shape not in (SHAPE_SINGLETON, SHAPE_LIST):
                        fields[key] = (field.outer_type_, _new_field_from_model_field(field))
                        continue

                    field_type, recreated_model = model_with_rel(
                        field.type_, c, __module__=__module__, __parent__module__=__parent__module__
                    )
                    if field.type_ != field.outer_type_:
                        field_type = getattr(typing, field.outer_type_._name)[field_type]

                    if field.allow_none:
                        field_type = Optional[field_type]

                    fields[key] = (field_type, _new_field_from_model_field(field))
                    if recreated_model:
                        recreate_model = True

                    try:
                        if issubclass(field_type, Reference):
                            recreate_model = True

                    except TypeError:
                        pass

                if issubclass(c, Reference):
                    recreate_model = True
                    value = Undefined
                    value_example = None
                    value_factory = None
                    if isinstance(c._rel, FunctionType):
                        value_factory = c._rel
                        value_example = c._rel()

                    else:
                        value = value_example = c._rel

                    fields['x_reference_key'] = (
                        str,
                        Field(
                            value,
                            example=value_example,
                            orm_field=None,
                            alias=reference_key,
                            default_factory=value_factory,
                        ),
                    )
                    if c._rel_params:
                        fields['x_reference_params_key'] = (
                            dict,
                            Field(alias=reference_params_key, orm_method=c._rel_params),
                        )

                if recreate_model:
                    if c not in _recreated_models:
                        _logger.debug(
                            "Recreate Model %s (in module %s)",
                            c,
                            c.__module__ if c.__module__ != __parent__module__ else __module__,
                        )
                        _recreated_models[c] = create_model(
                            f'{c.__qualname__} [R]',
                            __base__=(c, ReferencedModel),
                            __module__=c.__module__ if c.__module__ != __parent__module__ else __module__,
                            **fields,
                        )
                        _recreated_models[c].__recreated__ = True

                    if __parent__:
                        setattr(__parent__, c.__name__, _recreated_models[c])

                    return _recreated_models[c], True

            return c, False

        return model_with_rel(cls, None, __module__=cls.__module__, __parent__module__=cls.__base__.__module__)[0]

    return wrapped


def is_orm_field_set(field: FieldInfo) -> bool:
    if isinstance(field, ORMFieldInfo):
        orm_field = field.orm_field
        if orm_field is Undefined:
            return False

    else:
        orm_field = field.extra.get('orm_field')
        if 'orm_field' not in field.extra or ('orm_field' in field.extra and field.extra['orm_field'] is None):
            # Do not raise error when orm_field was explicitly set to None
            return False

    return True


def get_orm_field_attr(field: FieldInfo, key: str):
    if isinstance(field, ORMFieldInfo):
        return getattr(field, key)

    return field.extra.get(key)
