from locales import ru, tg

_LOCALES = {
    "ru": ru.TEXTS,
    "tg": tg.TEXTS,
}


def t(key: str, lang: str = "ru", **kwargs) -> str:
    pack = _LOCALES.get(lang) or _LOCALES["ru"]
    text = pack.get(key) or _LOCALES["ru"].get(key) or key
    if kwargs:
        return text.format(**kwargs)
    return text
