from decimal import Decimal
from pydantic import field_validator, BaseModel


TWO_PLACES = Decimal(10) ** -2


class AmountPrecision(BaseModel):
    gross: Decimal
    net: Decimal

    def __getitem__(self, name):
        return getattr(self, name)


class Amount(AmountPrecision):
    @field_validator('gross', 'net')
    @classmethod
    def _round_amount(cls, value: Decimal):
        return value.quantize(TWO_PLACES)
