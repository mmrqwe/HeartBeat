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
        "label": "小美女",
        "role": "小女生",
        "style": "元气满满，爱用感叹号，语气亲切",
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
}


def get_skin(name):
    return SKINS.get(name) or SKINS[DEFAULT_SKIN]


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
