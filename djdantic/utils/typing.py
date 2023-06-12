from typing import Type, TYPE_CHECKING, TypeVar, Generic
from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import ManyToManyRelatedManager


T = TypeVar('T')
M = TypeVar('M', bound=models.Model)


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
