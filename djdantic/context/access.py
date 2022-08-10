from contextvars import ContextVar
from ..schemas import Access


access: ContextVar[Access] = ContextVar('access')
