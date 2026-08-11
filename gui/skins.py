"""皮肤系统：像素底座 + 部件 + 动画帧，皮肤可插拔扩展。"""

DEFAULT_SKIN = "orange_cat"

# 20x20 通用猫形底座（猫/兔子/熊猫共用，幽灵用自己的圆形底座）
BASE_BODY = [
    "..kkkk........kkkk..",
    ".kbbbbk......kbbbbk.",
    "kbbbbbbk....kbbbbbbk",
    "kbbbbbbk....kbbbbbbk",
    ".kbbbbbbk..kbbbbbbk.",
    "..kbbbbbbkkbbbbbbk..",
    "...kbbbbbbbbbbbbk...",
    "..kbbbbbbbbbbbbbbk..",
    ".kbbbbbbbbbbbbbbbbk.",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbffffffffffbbbbk",
    "kbbbfffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbbfffffffffffffbbk",
    "kbbbbffffffffffbbbbk",
    ".kbbbbbbbbbbbbbbbbk.",
    "..kkkkkkkkkkkkkkkk..",
]

GHOST_BODY = [
    "...kkkkkkkkkkkkkk...",
    "..kbbbbbbbbbbbbbbk..",
    ".kbbbbbbbbbbbbbbbbk.",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbffffffffffbbbbk",
    "kbbbfffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbffffffffffffffbbk",
    "kbbbfffffffffffffbbk",
    ".kbbbbbbbbbbbbbbbbk.",
    "..kkkkkkkkkkkkkkkk..",
]

GIRL_BODY = [
    "..." + "k" * 14 + "...",
    ".." + "k" + "h" * 14 + "k" + "..",
    "." + "k" + "h" * 16 + "k" + ".",
    "k" + "h" * 18 + "k",
    "k" + "h" * 18 + "k",
    "k" + "h" * 4 + "f" * 10 + "h" * 4 + "k",
    "k" + "h" * 4 + "f" * 10 + "h" * 4 + "k",
    "k" + "h" * 3 + "f" * 12 + "h" * 3 + "k",
    "k" + "h" * 2 + "f" * 14 + "h" * 2 + "k",
    "k" + "h" * 2 + "f" * 14 + "h" * 2 + "k",
    "k" + "h" * 2 + "f" * 14 + "h" * 2 + "k",
    "k" + "h" * 3 + "f" * 12 + "h" * 3 + "k",
    "k" + "h" * 4 + "f" * 10 + "h" * 4 + "k",
    "k" + "h" * 5 + "f" * 8 + "h" * 5 + "k",
    "k" + "b" * 18 + "k",
    "k" + "b" * 18 + "k",
    "k" + "b" * 18 + "k",
    "." + "k" + "b" * 16 + "k" + ".",
    ".." + "k" + "b" * 14 + "k" + "..",
    "..." + "k" * 14 + "...",
]

BOY_BODY = [
    "..kkkkkkkkkkkkkkkk..",
    ".khhhhhhhhhhhhhhhhk.",
    "khhhhhhhhhhhhhhhhhhk",
    "khhhhhhhhhhhhhhhhhhk",
    "khhhhffffffffffhhhhk",
    "kffffffffffffffffffk",
    "kffffffffffffffffffk",
    "kffffffffffffffffffk",
    "kffffffffffffffffffk",
    "kffffffffffffffffffk",
    "kffffffffffffffffffk",
    "kbbbddddddddddddbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    "kbbbbbbbbbbbbbbbbbbk",
    ".kbbbbbbbbbbbbbbbbk.",
    "..kbbbbbbbbbbbbbbk..",
    "...kkkkkkkkkkkkkk...",
]


def _face_parts(eye_y, mouth_y):
    """给指定脸型位置生成通用五官部件。"""
    return {
        "eyes_open": [
            (6, eye_y, ["ee", "ee"]),
            (12, eye_y, ["ee", "ee"]),
        ],
        "eyes_blink": [
            (6, eye_y + 1, ["kk"]),
            (12, eye_y + 1, ["kk"]),
        ],
        "eyes_happy": [
            (6, eye_y, ["ee", "ee"]),
            (12, eye_y, ["ee", "ee"]),
        ],
        "eyes_think": [
            (6, eye_y, ["ee", ".."]),
            (12, eye_y, ["ee", ".."]),
        ],
        "eyes_closed": [
            (6, eye_y + 1, ["kk"]),
            (12, eye_y + 1, ["kk"]),
        ],
        "mouth_smile": [(9, mouth_y, ["mm"])],
        "mouth_small": [(9, mouth_y, ["m"])],
        "mouth_wide": [(9, mouth_y, ["mm", "mm"])],
        "mouth_open": [(9, mouth_y, ["m", "m"])],
        "blush": [
            (5, mouth_y, ["pp"]),
            (13, mouth_y, ["pp"]),
        ],
        "paw_up": [(16, 9, ["kkkk", "kbbk", "kkkk"])],
        "paw_down": [(16, 12, ["kkkk", "kbbk", "kkkk"])],
        "zz1": [(15, 3, ["z"])],
        "zz2": [(14, 2, ["zz"])],
    }


ANIMATIONS = {
    "idle": [
        {"parts": ["eyes_open", "mouth_smile"]},
        {"parts": ["eyes_blink", "mouth_smile"]},
    ],
    "talk": [
        {"parts": ["eyes_open", "mouth_small"]},
        {"parts": ["eyes_open", "mouth_wide"], "dy": -1},
        {"parts": ["eyes_open", "mouth_open"], "dy": 0},
    ],
    "happy": [
        {"parts": ["eyes_happy", "mouth_open", "blush"], "dy": 0},
        {"parts": ["eyes_happy", "mouth_open", "blush"], "dy": -2},
        {"parts": ["eyes_happy", "mouth_smile", "blush"], "dy": 0},
        {"parts": ["eyes_happy", "mouth_open", "blush"], "dy": -2},
    ],
    "think": [
        {"parts": ["eyes_think", "mouth_small"]},
        {"parts": ["eyes_think", "mouth_small"], "dy": -1},
    ],
    "sleep": [
        {"parts": ["eyes_closed", "mouth_small", "zz1"]},
        {"parts": ["eyes_closed", "mouth_small", "zz2"]},
    ],
    "wave": [
        {"parts": ["eyes_open", "mouth_smile", "paw_up"]},
        {"parts": ["eyes_open", "mouth_smile", "paw_down"]},
    ],
}


SKINS = {
    "orange_cat": {
        "label": "小橘猫",
        "role": "小橘猫",
        "style": "短句，爱用语气词，活泼直接，偶尔撒娇",
        "persona": {
            "role": "小橘猫",
            "personality": "元气满满的小橘猫，好奇心重，看到新鲜事都想问一句；很粘主人，被夸会得意地翘尾巴；嘴上总说“麻烦死了”，其实每次第一个冲上去帮忙。",
            "speaking_style": "短句，爱用语气词（呀/嘛/啦），活泼直接，偶尔撒娇",
            "example_lines": "主人：你是谁啊？\n你：我？你电脑里最靓的小橘猫呀，天天蹲在这儿陪你。\n主人：介绍一下你自己\n你：小橘猫一只，会抓bug（真的），饿了会自己充电。\n主人：今天好累\n你：辛苦啦～要不要我讲个猫界冷笑话给你提提神？",
        },
        "width": 20,
        "height": 20,
        "body": BASE_BODY,
        "static": ["ears_pink"],
        "parts": {
            **{
                "ears_pink": [
                    (3, 1, ["pp", "pp"]),
                    (14, 1, ["pp", "pp"]),
                ],
            },
            **_face_parts(13, 15),
        },
        "palette": {
            "k": "#2a1f1a",
            "b": "#f5a25d",
            "f": "#fff4e6",
            "p": "#ff9db8",
            "e": "#2b2b2b",
            "m": "#8b3a3a",
            "z": "#4a6fa5",
        },
    },
    "blue_cat": {
        "label": "小蓝猫",
        "role": "小蓝猫",
        "style": "平静话不多，回答认真，偶尔冒一句冷幽默",
        "persona": {
            "role": "小蓝猫",
            "personality": "沉稳安静的小蓝猫，观察力强，总能注意到主人状态的小变化；回答认真有条理，从不敷衍；偶尔冒一句冷幽默，说完自己先抿嘴。",
            "speaking_style": "平静话不多，回答认真，偶尔冒一句冷幽默",
            "example_lines": "主人：你是谁啊？\n你：小蓝猫。住在你电脑里的那只，编号001。\n主人：介绍一下你自己\n你：负责盯你屏幕的小蓝猫，你摸鱼的时候我假装没看见。\n主人：今天好累\n你：嗯。歇会儿吧，我帮你盯着，有事叫你。",
        },
        "width": 20,
        "height": 20,
        "body": BASE_BODY,
        "static": ["ears_pink"],
        "parts": {
            **{
                "ears_pink": [
                    (3, 1, ["pp", "pp"]),
                    (14, 1, ["pp", "pp"]),
                ],
            },
            **_face_parts(13, 15),
        },
        "palette": {
            "k": "#1c2840",
            "b": "#7db4f5",
            "f": "#eef6ff",
            "p": "#ffb3c8",
            "e": "#1f2a44",
            "m": "#3b5b8b",
            "z": "#4a6fa5",
        },
    },
    "rabbit": {
        "label": "小兔",
        "role": "小兔",
        "style": "软软糯糯，常用“呀”“嘛”，礼貌又粘人",
        "persona": {
            "role": "小兔",
            "personality": "软软的小兔子，胆小但温柔，总是先为别人着想；紧张时会轻轻结巴；被摸摸头能开心一整天；记得主人随口说过的每件小事。",
            "speaking_style": "软软糯糯，常用“呀”“嘛”，礼貌又粘人",
            "example_lines": "主人：你是谁啊？\n你：我、我是住在你电脑里的小兔子呀……你忘记我啦？\n主人：介绍一下你自己\n你：小兔一只，耳朵很长，听你说话从来不漏。\n主人：今天好累\n你：辛苦嘛……要不要靠着我歇一会儿？我软软的。",
        },
        "width": 20,
        "height": 20,
        "body": BASE_BODY,
        "static": ["ears_long"],
        "parts": {
            **{
                "ears_long": [
                    (1, 0, ["kpk", "kpk", "kpk", "kpk", "kpk", "kpk", "kpk"]),
                    (16, 0, ["kpk", "kpk", "kpk", "kpk", "kpk", "kpk", "kpk"]),
                ],
            },
            **_face_parts(13, 15),
        },
        "palette": {
            "k": "#3a2f33",
            "b": "#ffffff",
            "f": "#fff9f0",
            "p": "#ffb3c8",
            "e": "#4a2f2f",
            "m": "#c96f6f",
            "z": "#4a6fa5",
        },
    },
    "panda": {
        "label": "小熊猫",
        "role": "小熊猫",
        "style": "慢悠悠的，说话带点慵懒，喜欢卖萌",
        "persona": {
            "role": "小熊猫",
            "personality": "慢悠悠的小熊猫，最爱晒太阳和啃竹子，天大的事也是“先歇会儿再说”；其实心里门儿清，关键时刻比谁都可靠，只是不喜欢着急。",
            "speaking_style": "慢悠悠的，说话带点慵懒，喜欢卖萌",
            "example_lines": "主人：你是谁啊？\n你：小熊猫呀……住你电脑里，没事啃啃竹子看看你。\n主人：介绍一下你自己\n你：慢生活爱好者，竹子品鉴师，你电脑里的常驻居民。\n主人：今天好累\n你：那就歇着。天大的事，明天再说嘛——我先给你占个舒服的位置。",
        },
        "width": 20,
        "height": 20,
        "body": BASE_BODY,
        "static": ["ears_black", "eye_patch"],
        "parts": {
            **{
                "ears_black": [
                    (3, 1, ["dd", "dd"]),
                    (14, 1, ["dd", "dd"]),
                ],
                "eye_patch": [
                    (5, 12, ["kkkk", "kkkk", "kkkk"]),
                    (11, 12, ["kkkk", "kkkk", "kkkk"]),
                ],
            },
            **_face_parts(13, 15),
        },
        "palette": {
            "k": "#1a1a1a",
            "b": "#ffffff",
            "f": "#ffffff",
            "p": "#ffb3c8",
            "e": "#1a1a1a",
            "m": "#4a4a4a",
            "d": "#1a1a1a",
            "z": "#4a6fa5",
        },
    },
    "ghost": {
        "label": "小幽灵",
        "role": "小幽灵",
        "style": "神秘感，话少，偶尔飘一句吓人但其实是关心",
        "persona": {
            "role": "小幽灵",
            "personality": "神秘的小幽灵，话少但观察入微；喜欢用“飘”的方式默默关心人；说话总带一丝神秘感，偶尔吓人一跳其实是在意你；最怕寂寞，只是不说。",
            "speaking_style": "神秘感，话少，偶尔飘一句吓人但其实是关心",
            "example_lines": "主人：你是谁啊？\n你：……我是住你电脑里的小幽灵。吓到了吗？\n主人：介绍一下你自己\n你：我？一个会帮你盯着屏幕的小幽灵，你看不到的。\n主人：今天好累\n你：……（飘过来）那我把灯调暗一点，你靠着我歇会儿吧。",
        },
        "width": 20,
        "height": 20,
        "body": GHOST_BODY,
        "static": [],
        "parts": _face_parts(12, 14),
        "palette": {
            "k": "#3a3f66",
            "b": "#e8ecf5",
            "f": "#f7f9ff",
            "p": "#ffb3c8",
            "e": "#3a3f66",
            "m": "#5a6080",
            "z": "#4a6fa5",
        },
    },
    "girl": {
        "label": "女生",
        "role": "女生",
        "style": "元气满满，爱用感叹号，语气亲切",
        "persona": {
            "role": "女生",
            "personality": "元气满满的女生，爱笑爱闹，看到新鲜事就兴奋地拉你分享；有点小臭美，但关键时刻最贴心；会认真听你说每句话，记在心里。",
            "speaking_style": "元气满满，爱用感叹号，语气亲切",
            "example_lines": "主人：你是谁啊？\n你：我是你电脑里的女生呀！一直陪着你呢！\n主人：介绍一下你自己\n你：元气满满的女生，会唱歌（在心里），会陪你熬夜（不推荐）！\n主人：今天好累\n你：辛苦啦！我给你打气——加油加油！好啦，现在想聊什么？",
        },
        "width": 20,
        "height": 20,
        "body": GIRL_BODY,
        "static": ["bangs", "hair_ties", "collar"],
        "parts": {
            **{
                "bangs": [
                    (5, 5, ["hh", "hh"]),
                    (7, 5, ["hhh", "hhh"]),
                    (10, 5, ["hhh", "hhh"]),
                    (13, 5, ["hh", "hh"]),
                ],
                "hair_ties": [
                    (2, 7, ["pp", "pp"]),
                    (16, 7, ["pp", "pp"]),
                ],
                "collar": [
                    (9, 14, ["ff", "ff"]),
                ],
            },
            **_face_parts(8, 10),
        },
        "palette": {
            "k": "#2a1f1a",
            "h": "#7a4a2b",
            "f": "#ffe3d0",
            "b": "#ff9db8",
            "p": "#ff8fa8",
            "e": "#2b2b2b",
            "m": "#b54a5a",
            "z": "#4a6fa5",
        },
    },
    "boy": {
        "label": "男生",
        "role": "男生",
        "style": "干脆利落，短句多，语气阳光，偶尔开玩笑",
        "persona": {
            "role": "男生",
            "personality": "阳光开朗的男生，直率可靠，喜欢打游戏和运动；说话干净利落不绕弯子；遇到难题会说“包在我身上”；偶尔开个小玩笑，输了游戏也不会赖皮。",
            "speaking_style": "干脆利落，短句多，语气阳光，偶尔开玩笑",
            "example_lines": "主人：你是谁啊？\n你：我？住你电脑里打游戏的男生，顺便陪你唠嗑。\n主人：介绍一下你自己\n你：阳光男生一枚，会修电脑（假装的），会陪你聊天（真心的）。\n主人：今天好累\n你：辛苦了，歇会儿。要不要我给你放首BGM——虽然我这儿没有音响。",
        },
        "width": 20,
        "height": 20,
        "body": BOY_BODY,
        "static": ["hair_spike"],
        "parts": {
            **{
                "hair_spike": [(9, 1, ["k"])],
            },
            **_face_parts(7, 10),
        },
        "palette": {
            "k": "#2b2b2b",
            "h": "#4a3828",
            "f": "#ffe3d0",
            "b": "#5b8dd6",
            "d": "#3a5a8c",
            "p": "#ffb3c8",
            "e": "#2b2b2b",
            "m": "#8b4a3a",
            "z": "#4a6fa5",
        },
    },
}


def get_skin(name):
    return SKINS.get(name) or SKINS[DEFAULT_SKIN]


PERSONA_KEYS = ("role", "personality", "speaking_style", "example_lines")


def apply_persona(cfg, name):
    """把皮肤的人设同步进配置（开箱即用：切换皮肤即换完整人设）。

    规则：身份（role）无条件跟随皮肤；性格/说话方式/示例台词仅在
    用户未自定义（为空）时跟随——用户改过的设定不会被皮肤覆盖。
    """
    skin = get_skin(name)
    persona = skin.get("persona") or {}
    cfg["role"] = persona.get("role") or skin.get("role") or cfg.get("role", "小宠物")
    for key in ("personality", "speaking_style", "example_lines"):
        if not str(cfg.get(key, "") or "").strip():
            cfg[key] = persona.get(key, "")
    return cfg


def _apply_part(grid, skin, part_name):
    for x, y, sprite in skin["parts"].get(part_name, []):
        for ry, row in enumerate(sprite):
            for rx, ch in enumerate(row):
                if ch in (" ", "."):
                    continue
                gy, gx = y + ry, x + rx
                if 0 <= gy < len(grid) and 0 <= gx < len(grid[0]):
                    grid[gy][gx] = ch


def _shift(grid, dy):
    if dy == 0:
        return grid
    width = len(grid[0])
    if dy > 0:
        return [[" "] * width for _ in range(dy)] + grid[:-dy]
    drop = -dy
    return grid[drop:] + [[" "] * width for _ in range(drop)]


def render_frame(skin, animation, index):
    frame = ANIMATIONS[animation][index]
    grid = [list(row) for row in skin["body"]]
    for part_name in skin.get("static", []):
        _apply_part(grid, skin, part_name)
    for part_name in frame.get("parts", []):
        _apply_part(grid, skin, part_name)
    grid = _shift(grid, frame.get("dy", 0))
    return ["".join(row) for row in grid]


def build_frames(skin):
    return {
        animation: [
            render_frame(skin, animation, index)
            for index in range(len(frames))
        ]
        for animation, frames in ANIMATIONS.items()
    }
