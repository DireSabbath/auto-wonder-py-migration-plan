"""稳定 ``X.Y.Z`` 版本比较。解析不了的版本视为未知，不当作更旧。"""

_LONG_MAX = 9223372036854775807


def compare_versions(reported: str | None, target: str | None) -> int | None:
    """负数表示上报版本落后于目标。任一侧不是稳定三段版本时返回空。"""
    left = _parse(reported)
    right = _parse(target)
    if left is None or right is None:
        return None
    for index in range(3):
        if left[index] != right[index]:
            return (left[index] > right[index]) - (left[index] < right[index])
    return 0


def is_behind(reported: str | None, target: str | None) -> bool:
    """只有两边都能解析、且上报版本严格更低时才算落后。"""
    comparison = compare_versions(reported, target)
    if comparison is None:
        return False
    return comparison < 0


def _parse(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    trimmed = value.strip()
    if trimmed.startswith(("v", "V")):
        trimmed = trimmed[1:]
    parts = trimmed.split(".")
    if len(parts) != 3:
        return None
    numbers: list[int] = []
    for part in parts:
        if part == "" or len(part) > 19:
            return None
        number = 0
        for char in part:
            if char < "0" or char > "9":
                return None
            digit = ord(char) - 48
            if number > (_LONG_MAX - digit) // 10:
                return None
            number = number * 10 + digit
        numbers.append(number)
    return numbers[0], numbers[1], numbers[2]
