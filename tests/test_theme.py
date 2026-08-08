"""
Theme tests.

A theme is only useful if every one of them is complete: a missing token
renders as `var(--fm-whatever)` with no fallback, which browsers resolve to
nothing — invisible text on an unpainted ground. These pin completeness,
uniqueness, and the one property the retune was for: colour means something.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import theme


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8").read()


# =====================================================================
# Completeness
# =====================================================================

def test_the_default_theme_exists():
    assert theme.DEFAULT in theme.THEMES
    assert theme.DEFAULT == "terminal", "Terminal is the chosen default"


def test_every_theme_defines_every_token():
    """
    A token missing from one theme resolves to nothing at all -- CSS variables
    have no implicit fallback, so the rule is dropped and the element renders
    unpainted rather than wrong.
    """
    required = set(theme.THEMES[theme.DEFAULT]["tokens"])
    for name, spec in theme.THEMES.items():
        missing = required - set(spec["tokens"])
        assert not missing, f"{name} is missing {sorted(missing)}"
        assert not any(v in (None, "") for v in spec["tokens"].values()), name


def test_every_theme_has_a_label_and_a_blurb():
    """The settings panel prints both; a missing one shows as an empty row."""
    for name, spec in theme.THEMES.items():
        assert spec["label"].strip()
        assert len(spec["blurb"]) > 30, f"{name} needs a real description"


def test_every_theme_supplies_both_overlay_ramps():
    for name, spec in theme.THEMES.items():
        for key in ("ramp_neutral", "ramp_accent"):
            ramp = spec[key]
            assert len(ramp) >= 5, f"{name}.{key} must cover the overlay count"
            assert len(set(ramp)) == len(ramp), f"{name}.{key} repeats a colour"


def test_every_stylesheet_variable_is_defined():
    """Scan the shared sheet for var(--fm-*) and check each one is emitted."""
    used = set(re.findall(r"var\((--fm-[a-z-]+)\)", theme._SHARED_CSS))
    for name in theme.THEMES:
        emitted = set(re.findall(r"^\s*(--fm-[a-z-]+):", theme.css(name), re.M))
        assert used <= emitted, f"{name} never defines {sorted(used - emitted)}"


# =====================================================================
# Distinctness — three themes that look the same are one theme
# =====================================================================

def test_the_themes_are_actually_different():
    grounds = {n: s["tokens"]["bg"] for n, s in theme.THEMES.items()}
    accents = {n: s["tokens"]["accent"] for n, s in theme.THEMES.items()}
    assert len(set(grounds.values())) == len(grounds)
    assert len(set(accents.values())) == len(accents)


def test_the_terminal_theme_uses_a_monospaced_face_for_figures():
    """Digits have to land in the same column; that is the whole point."""
    assert "mono" in theme.tokens("terminal")["font_num"].lower()


def test_no_theme_reuses_its_accent_as_a_direction_colour():
    """
    If the system colour is also the up or down colour, a rising price and a
    UI chrome element become indistinguishable.
    """
    for name, spec in theme.THEMES.items():
        t = spec["tokens"]
        assert t["accent"] not in (t["up"], t["down"]), name


# =====================================================================
# Resolution
# =====================================================================

@pytest.mark.parametrize("name", list(theme.THEMES))
def test_a_known_theme_resolves_to_itself(name):
    assert theme.resolve(name) == name


def test_an_unknown_theme_falls_back_rather_than_raising():
    """A stale session_state value must not take the dashboard down."""
    assert theme.resolve("nope") == theme.DEFAULT
    assert theme.resolve("") == theme.DEFAULT
    assert theme.resolve(None) == theme.DEFAULT


def test_css_renders_for_every_theme_and_density():
    for name in theme.THEMES:
        for density in theme.DENSITIES:
            sheet = theme.css(name, density)
            assert sheet.startswith("<style>") and sheet.endswith("</style>")
            assert theme.tokens(name)["accent"] in sheet


def test_an_unknown_density_falls_back_to_compact():
    assert theme.css("terminal", "nope") == theme.css("terminal", "compact")


# =====================================================================
# Chart palette
# =====================================================================

@pytest.mark.parametrize("name", list(theme.THEMES))
def test_the_chart_palette_is_complete(name):
    required = {"paper", "plot", "ink", "dim", "faint", "accent", "up", "down",
                "font", "grid", "axis", "ramp", "overlay", "band", "band_faint",
                "accent_band", "accent_band_faint", "accent_band_faintest",
                "up_fill", "down_fill"}
    assert required <= set(theme.chart(name))


def test_the_overlay_ramp_cycles_rather_than_running_out():
    """More overlays than ramp entries must not IndexError mid-render."""
    pal = theme.chart("terminal")
    assert pal["overlay"](0) == pal["overlay"](len(pal["ramp"]))
    assert all(pal["overlay"](i) for i in range(40))


def test_neutral_and_accent_overlay_styles_differ():
    assert theme.chart("terminal", "neutral")["ramp"] != theme.chart("terminal", "accent")["ramp"]


def test_an_unknown_overlay_style_falls_back_to_neutral():
    assert theme.chart("terminal", "nope")["ramp"] == theme.chart("terminal", "neutral")["ramp"]


def test_a_band_edge_is_more_visible_than_its_fill():
    """
    One shared alpha made Bollinger edges invisible and their legend swatch
    blank. The edge has to read as a boundary; the fill must not compete.
    """
    def alpha(rgba):
        return float(rgba.rsplit(",", 1)[1].rstrip(")"))

    for name in theme.THEMES:
        pal = theme.chart(name)
        assert alpha(pal["band"]) > alpha(pal["band_faint"]) * 3, name
        assert alpha(pal["accent_band_faint"]) > alpha(pal["accent_band_faintest"]), (
            f"{name}: the 68% forecast cone must read denser than the 95%")


def test_rgba_conversion_is_correct():
    pal = theme.chart("terminal")          # dim is #7A7A7A -> 122
    assert pal["band"].startswith("rgba(122,122,122,")


# =====================================================================
# The app must not go behind the theme's back
# =====================================================================

def test_no_hardcoded_chart_colour_survives_in_the_dashboard():
    """
    Six competing hues inside one figure — EMA 21 and EMA 9 in two pinks, RSI
    purple, MACD blue — none of which meant anything. Every colour now comes
    from the palette, and a new literal fails here.
    """
    offenders = [
        f"app.py:{i}" for i, line in enumerate(APP.splitlines(), 1)
        if ('color="#' in line or 'color="rgba' in line)
    ]
    assert not offenders, f"use PALETTE[...] instead of a literal at {offenders}"


def test_the_sponsor_card_is_gone():
    """It sat directly beneath the controls that size a position."""
    for phrase in ("Buy Me a Coffee", "Support Open Source", "github.com/sponsors"):
        assert phrase not in APP


def test_the_settings_control_is_wired_to_the_session_keys():
    """The stylesheet is injected from session_state before the widget renders."""
    for key in ("ui_theme", "ui_density", "ui_overlays"):
        assert f'st.session_state.setdefault("{key}"' in APP, f"{key} has no default"
        assert f'key="{key}"' in APP, f"{key} has no widget"


def test_the_stylesheet_is_injected_before_the_first_widget():
    """Otherwise the page paints unstyled, then repaints — a visible flash."""
    css_at = APP.index("fm_theme.css(")
    assert css_at < APP.index("st.sidebar.markdown")
    assert css_at < APP.index("st.tabs(")


# =====================================================================
# No emoji on any user-visible surface
# =====================================================================
# Pictographs standing in for words read as decoration rather than as an
# instrument, and a glyph that fails to load silently changes the sentence.
# Real typography stays: the em dash, the rightwards arrow, the middot.

_EMOJI_RANGES = [
    (0x1F000, 0x1FAFF),   # pictographs, emoticons, symbols
    (0x2300, 0x23FF),     # misc technical (gear, hourglass)
    (0x2460, 0x24FF),     # circled digits
    (0x25A0, 0x2BFF),     # geometric shapes, dingbats, misc symbols
    (0xFE0F, 0xFE0F),     # variation selector
]
_ALLOWED = {"→", "←", "↔"}   # arrows used as punctuation


def _emoji_in(text):
    return {c for c in text
            if c not in _ALLOWED
            and any(lo <= ord(c) <= hi for lo, hi in _EMOJI_RANGES)}


@pytest.mark.parametrize("relpath", [
    "dashboard/app.py", "dashboard/theme.py", "dashboard/alert_watcher.py",
    "dashboard/webull_client.py", "finance_mcp.py", "installer.ps1", "README.md",
])
def test_no_emoji_on_a_user_visible_surface(relpath):
    text = open(os.path.join(ROOT, relpath), encoding="utf-8").read()
    found = _emoji_in(text)
    assert not found, f"{relpath} contains {sorted(hex(ord(c)) for c in found)}"


def test_provenance_markers_are_words_not_pictures():
    """They carry how much a number can be trusted; that must always render."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_fm", os.path.join(ROOT, "finance_mcp.py"))
    src = open(os.path.join(ROOT, "finance_mcp.py"), encoding="utf-8").read()
    block = src.split("PROVENANCE = {", 1)[1].split("}", 1)[0]
    for tag in ("[FILED]", "[EXACT]", "[MARKET]", "[OFFICIAL]",
                "[THIRD-PARTY]", "[ESTIMATE]"):
        assert tag in block, f"provenance marker {tag} is missing"


def test_the_deploy_button_is_suppressed():
    """
    Streamlit's toolbar offers 'Deploy', which pushes to Streamlit Community
    Cloud -- a public host. On a dashboard wired to a live brokerage account
    that button has no safe outcome.
    """
    config = open(os.path.join(ROOT, ".streamlit", "config.toml"), encoding="utf-8").read()
    assert "toolbarMode" in config and "minimal" in config
    assert 'stAppDeployButton' in theme._SHARED_CSS
