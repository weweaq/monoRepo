"""画像变化检测。

从输出目录读取上次 json 文件，与本次画像做 diff。
不写数据库，变化结果直接传给 reporter 生成 md。
"""


def diff_profiles(old: dict, new: dict, profile_type: str = "global", path_prefix: str = "") -> list[dict]:
    """递归对比两个字典，返回变化列表。"""
    changes = []

    for key, new_value in new.items():
        full_key = f"{path_prefix}.{key}" if path_prefix else key
        old_value = old.get(key)

        if isinstance(new_value, dict) and isinstance(old_value, dict):
            changes.extend(diff_profiles(old_value, new_value, profile_type, full_key))
        else:
            change = _compare(profile_type, full_key, old_value, new_value)
            if change:
                changes.append(change)

    for key in old:
        if key not in new:
            full_key = f"{path_prefix}.{key}" if path_prefix else key
            changes.append({
                "profile_type": profile_type,
                "change_type": "disappeared",
                "field_name": full_key,
                "old_value": str(old[key])[:200],
                "new_value": "",
                "change_summary": f"字段 `{full_key}` 消失",
            })

    return changes


def _compare(profile_type: str, key: str, old, new) -> dict | None:
    if old is None:
        return {
            "profile_type": profile_type,
            "change_type": "new",
            "field_name": key,
            "old_value": "",
            "new_value": str(new)[:200],
            "change_summary": f"新增 `{key}`: {str(new)[:60]}",
        }

    if old == new:
        return None

    change_type = "changed"
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if new > old:
            change_type = "increased"
        elif new < old:
            change_type = "decreased"

    return {
        "profile_type": profile_type,
        "change_type": change_type,
        "field_name": key,
        "old_value": str(old)[:200],
        "new_value": str(new)[:200],
        "change_summary": f"`{key}` 从 `{str(old)[:60]}` 变为 `{str(new)[:60]}`",
    }
