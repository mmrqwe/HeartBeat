"""brain.skills：技能包元数据解析（可进化域，纯函数）。

从 core.py 拆分（阶段1）：SKILL.md frontmatter 的 name/description 提取，
只注入元数据（防 prompt injection 的最小信息面）。
"""

import re

SKILL_NAME_MAX = 40    # 技能名截断
SKILL_DESC_MAX = 200   # 技能描述截断（注入 system prompt 的最小元数据面）


def parse_skill_frontmatter(text):
    """从 SKILL.md 提取 frontmatter 的 name/description（仅元数据）。

    安全设计（架构评审 2026-08-12）：技能包是外部不可信内容，注入宠物
    system prompt 前只保留这两个字段，丢弃其余全部内容；去控制字符、
    按长度截断，防止 description 内嵌指令文本进入上下文。
    支持 YAML 折叠多行（description: >- 后跟缩进行）。解析失败返回 {}。
    """
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    data = {}
    desc_parts = []
    in_desc = False
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t")):
            if in_desc:
                desc_parts.append(line.strip())
            continue
        in_desc = False
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "name":
            data["name"] = re.sub(r"[\x00-\x1f\x7f]", "", value.strip())[:SKILL_NAME_MAX]
        elif key == "description":
            in_desc = True
            if value.strip():
                desc_parts.append(value.strip())
    if desc_parts:
        desc = re.sub(r"[\x00-\x1f\x7f]", "", " ".join(desc_parts)).strip()
        if desc:
            data["description"] = desc[:SKILL_DESC_MAX]
    return data
