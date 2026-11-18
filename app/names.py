import re

# A letter that starts a word: at the very start, or after a space, hyphen, apostrophe or dot.
_WORD_START = re.compile(r"(^|[\s\-'’.])([a-z])")


def person_name(value: str) -> str:
    """A person's name as Denti shows it everywhere: single spaces, and the first letter of every
    word a capital ("dr meera nair" gives "Dr Meera Nair", "d'souza" gives "D'Souza"). Every other
    letter stays as typed, so "McDonald" keeps its capital D."""
    tidy = " ".join(value.split())
    return _WORD_START.sub(lambda m: m.group(1) + m.group(2).upper(), tidy)
