"""Ukrainian Merchant Category Code (MCC) catalog and category resolver."""

from __future__ import annotations

MCC_CATEGORIES: dict[int, str] = {
    5411: "Продукти",
    5499: "Продукти",
    5441: "Продукти",
    5812: "Кафе та ресторани",
    5813: "Кафе та ресторани",
    5814: "Кафе та ресторани",
    4121: "Транспорт та авто",
    4111: "Транспорт та авто",
    4112: "Транспорт та авто",
    5541: "Транспорт та авто",
    5542: "Транспорт та авто",
    4784: "Транспорт та авто",
    4814: "Зв'язок та інтернет",
    4816: "Зв'язок та інтернет",
    5912: "Аптеки та здоров'я",
    8011: "Аптеки та здоров'я",
    8062: "Аптеки та здоров'я",
    5734: "Підписки та сервіси",
    5815: "Підписки та сервіси",
    5816: "Підписки та сервіси",
    5818: "Підписки та сервіси",
    7841: "Підписки та сервіси",
    4899: "Підписки та сервіси",
    4215: "Пошта та доставка",
    5732: "Техніка та електроніка",
    5722: "Техніка та електроніка",
    5651: "Одяг та взуття",
    5691: "Одяг та взуття",
    5661: "Одяг та взуття",
    7997: "Спорт та фітнес",
    5942: "Покупки та хобі",
    5999: "Покупки та хобі",
    5311: "Покупки та хобі",
    4829: "Перекази",
    6012: "Перекази",
}

_SUBSCRIPTION_KEYWORDS: tuple[str, ...] = (
    "netflix",
    "spotify",
    "youtube",
    "icloud",
    "apple.com/bill",
    "chatgpt",
    "openai",
    "cursor",
    "telegram premium",
    "megogo",
    "patreon",
)

_FOOD_DELIVERY_KEYWORDS: tuple[str, ...] = (
    "glovo",
    "bolt food",
    "wolt",
)

_POSTAL_KEYWORDS: tuple[str, ...] = (
    "нова пошта",
    "nova poshta",
    "укрпошта",
    "ukrposhta",
)


def resolve_mcc_category(
    mcc: int,
    description: str = "",
    amount_minor: int = -1,
) -> str:
    """Resolve a normalized Ukrainian category name from amount, description, and MCC."""
    if amount_minor > 0:
        return "Дохід та поповнення"

    desc = (description or "").lower()

    if any(keyword in desc for keyword in _SUBSCRIPTION_KEYWORDS):
        return "Підписки та сервіси"

    if any(keyword in desc for keyword in _FOOD_DELIVERY_KEYWORDS):
        return "Кафе та ресторани"

    if any(keyword in desc for keyword in _POSTAL_KEYWORDS):
        return "Пошта та доставка"

    return MCC_CATEGORIES.get(mcc, "Інше")
