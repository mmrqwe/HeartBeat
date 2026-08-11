"""皮肤系统测试：结构完整性、帧有效性、动画丰富度。"""

import inspect
import json

from gui import skins


PERSONA_KEYS = ("role", "personality", "speaking_style", "example_lines")


def test_default_skin_exists():
    assert skins.DEFAULT_SKIN in skins.SKINS


def test_at_least_five_skins():
    assert len(skins.SKINS) >= 5


def test_get_skin_fallback():
    assert skins.get_skin("not_exist") is skins.SKINS[skins.DEFAULT_SKIN]
    assert skins.get_skin("rabbit") is skins.SKINS["rabbit"]


def test_every_skin_valid_structure():
    for name, skin in skins.SKINS.items():
        width = skin["width"]
        assert len(skin["body"]) == skin["height"]
        for row in skin["body"]:
            assert len(row) == width, f"{name} body row length {len(row)} != {width}"
        for part_name, placements in skin["parts"].items():
            for x, y, sprite in placements:
                assert x >= 0 and y >= 0
                for row in sprite:
                    assert len(row) == len(sprite[0])


def test_every_frame_valid():
    for name, skin in skins.SKINS.items():
        frames = skins.build_frames(skin)
        assert set(frames) == set(skins.ANIMATIONS)
        for animation, frame_list in frames.items():
            assert len(frame_list) >= 2, f"{name}/{animation} 帧数不足"
            for grid in frame_list:
                assert len(grid) == skin["height"]
                for row in grid:
                    assert len(row) == skin["width"]
                    for ch in row:
                        assert ch in (" ", ".") or ch in skin["palette"], (
                            f"{name}/{animation} 出现未知字符 {ch!r}"
                        )


def test_animations_have_rich_set():
    required = {"idle", "talk", "happy", "think", "sleep", "wave"}
    assert required <= set(skins.ANIMATIONS)


def test_happy_frames_move():
    skin = skins.get_skin("orange_cat")
    frames = skins.build_frames(skin)["happy"]
    assert len(set(tuple(f) for f in frames)) >= 3


def test_sleep_has_zzz():
    for name in skins.SKINS:
        grid = skins.render_frame(skins.SKINS[name], "sleep", 0)
        assert any("z" in row for row in grid), f"{name} sleep 缺少 Zzz"


def test_panda_has_patches():
    grid = skins.render_frame(skins.SKINS["panda"], "idle", 0)
    flat = "".join(grid)
    assert flat.count("d") > 0  # 黑色耳朵
    assert "kkkk" in flat  # 眼圈


def test_rabbit_has_long_ears():
    grid = skins.render_frame(skins.SKINS["rabbit"], "idle", 0)
    assert "kpk" in grid[0] or "kpk" in grid[1]


def test_ghost_has_no_ears():
    grid = skins.render_frame(skins.SKINS["ghost"], "idle", 0)
    assert "kkkkkkkkkk" in grid[0]


def test_girl_features():
    skin = skins.SKINS["girl"]
    assert skin["label"] == "女生"
    grid = skins.render_frame(skin, "idle", 0)
    flat = "".join(grid)
    assert "h" in flat      # 头发
    assert "f" in flat      # 肤色
    assert "b" in flat      # 裙子
    assert "pp" in flat     # 发饰/腮红


def test_girl_animations_render():
    frames = skins.build_frames(skins.SKINS["girl"])
    for animation in ("idle", "talk", "happy", "think", "sleep", "wave"):
        assert len(frames[animation]) >= 2


def test_every_skin_has_role():
    for name, skin in skins.SKINS.items():
        assert skin.get("role"), f"{name} 缺少角色设定"
    assert skins.SKINS["girl"]["role"] == "女生"
    assert skins.SKINS["orange_cat"]["role"] == "小橘猫"


def test_every_skin_has_persona():
    """每个皮肤都有完整人设（角色/性格/说话方式/示例台词），开箱即用。"""
    for name, skin in skins.SKINS.items():
        persona = skin.get("persona") or {}
        for key in PERSONA_KEYS:
            assert str(persona.get(key, "") or "").strip(), f"{name} 缺少 persona.{key}"
        assert persona["role"] == skin["role"], f"{name} persona.role 与顶层 role 不一致"


def test_boy_features():
    """男生皮肤：短发/衬衫/领带 + 阳光人设。"""
    skin = skins.SKINS["boy"]
    assert skin["label"] == "男生"
    assert skin["persona"]["role"] == "男生"
    grid = skins.render_frame(skin, "idle", 0)
    flat = "".join(grid)
    assert "h" in flat  # 头发
    assert "d" in flat  # 领带
    assert "b" in flat  # 衬衫
    assert "f" in flat  # 肤色
    assert skin["body"] is skins.BOY_BODY


def test_apply_persona_sync():
    """apply_persona：身份无条件覆盖；性格/说话方式/示例仅未自定义时跟随。"""
    cfg = {"role": "旧角色", "personality": "", "speaking_style": "", "example_lines": ""}
    skins.apply_persona(cfg, "boy")
    assert cfg["role"] == "男生"
    assert "阳光" in cfg["personality"]
    assert "干脆利落" in cfg["speaking_style"]
    assert "男生" in cfg["example_lines"]
    # 用户自定义过的字段保留，身份仍跟随皮肤
    cfg2 = {
        "role": "旧角色",
        "personality": "我的自定义性格",
        "speaking_style": "我的风格",
        "example_lines": "自定义示例",
    }
    skins.apply_persona(cfg2, "boy")
    assert cfg2["role"] == "男生"
    assert cfg2["personality"] == "我的自定义性格"
    assert cfg2["speaking_style"] == "我的风格"
    assert cfg2["example_lines"] == "自定义示例"


def test_persona_flows_to_build_persona():
    """人设进入 LLM prompt（开箱即用：切皮肤即换完整人设）。"""
    import core

    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    skins.apply_persona(cfg, "boy")
    prompt = core.build_persona(cfg)
    assert "男生" in prompt
    assert "阳光" in prompt


def _run_plain():
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    _run_plain()
