"""Named ad-style prompt library.

Every style is a deterministic template: the operator's offer fields
(headline / sub-headline / also-include / don't-include) are slotted in
verbatim by code, so dollar amounts, percentages, and time periods can
never drift the way they can when an LLM paraphrases the brief.

Two families:

  designed — the operator's proven ChatGPT prompts used VERBATIM, with
             only the slots swapped in: client name, system, the offer
             lines (Headline / Sub-Headline / Feature / Also feature),
             don't-include lines, and a generic house wording where the
             originals named client cities. Do NOT "improve" these —
             rewording or adding rule blocks measurably degrades output.
  organic  — native-feeling non-ad styles (POV body cam, ugly marker,
             product close-up) ported from the Claude pipeline prompt.

Public API:
  STYLES            ordered dict of style_key -> Style
  SETTINGS          ordered dict of setting_key -> label
  build_prompt()    render one style into a final gpt-image-1 prompt
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Offer context ──────────────────────────────────────────────────────────────

@dataclass
class OfferContext:
    client_name: str
    system_name: str                    # e.g. "Lennox System" (image filename stem)
    headline: str                       # main offer — the dominant text
    subheadline: str = ""               # secondary offer
    features: list[str] = field(default_factory=list)   # "also include" items
    dont_include: list[str] = field(default_factory=list)
    callout: str = ""                   # city/region — only used by ugly_marker geo-gate
    setting: str = "vary"               # SETTINGS key
    logo_mode: str = "overlay"          # "ai" | "overlay" | "none"
    variant: int = 0                    # 0-based repeat index of THIS style in a run
    image_index: int = 0                # 0-based global position in the run


def brand_name(system_name: str) -> str:
    """System name for prose slots. Strips a trailing 'system'/'unit' word so
    a file named 'Lennox System.png' yields 'Lennox system', not
    'Lennox System system'."""
    t = (system_name or "").strip()
    for suffix in (" system", " unit"):
        if t.lower().endswith(suffix):
            t = t[: -len(suffix)].strip()
    return t or system_name


def _brand(ctx: "OfferContext") -> str:
    return brand_name(ctx.system_name)


def _q(text: str) -> str:
    """Quote user text for the prompt. Unwraps at most one symmetric pair of
    user-pasted wrapper quotes; internal quotes are preserved verbatim."""
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1].strip()
    return '"' + t + '"'


# ── Scene settings (generic — replaces hard-coded client locations) ───────────

SETTINGS: dict[str, str] = {
    "vary":     "Vary across images",
    "suburban": "Modern suburban home",
    "luxury":   "Luxury estate",
    "beach":    "Beach house",
    "country":  "Country house",
    "mountain": "Mountain home",
}

_SETTING_SCENES: dict[str, str] = {
    "suburban": ("a beautiful modern suburban house with crisp siding, tidy "
                 "landscaping, and a clean side yard"),
    "luxury":   ("a luxurious upscale home with a pool, a stone patio, and a "
                 "polished high-end exterior"),
    "beach":    ("a bright, airy beach house with light coastal landscaping "
                 "and a breezy, sunlit feel"),
    "country":  ("a charming country house with mature trees, a generous "
                 "green yard, and a warm welcoming exterior"),
    "mountain": ("a cozy upscale mountain home with timber accents and "
                 "evergreen surroundings"),
}

_SETTING_ROTATION = ["suburban", "luxury", "beach", "country", "mountain"]

# Short generic house nouns spliced into the operator's original sentences
# in place of "Palm Beach County home" / "vancouver area" / "Calgary home".
_SETTING_NOUNS: dict[str, str] = {
    "suburban": "modern suburban home",
    "luxury":   "luxury home",
    "beach":    "beach house",
    "country":  "country house",
    "mountain": "mountain home",
}


def _setting_key(ctx: OfferContext) -> str:
    if ctx.setting in _SETTING_NOUNS:
        return ctx.setting
    # "Vary" rotates per image across the whole run. Adding `variant` keeps
    # repeats of the same style from landing on the same setting when the
    # style count divides evenly into the rotation length.
    return _SETTING_ROTATION[(ctx.image_index + ctx.variant) % len(_SETTING_ROTATION)]


def _scene_for(ctx: OfferContext) -> str:
    return _SETTING_SCENES[_setting_key(ctx)]


def _noun_for(ctx: OfferContext) -> str:
    return _SETTING_NOUNS[_setting_key(ctx)]


# ── Shared blocks (designed family — operator's exact prompt format) ──────────

def _offer_lines(ctx: OfferContext) -> str:
    """The offer, formatted exactly like the operator's manual prompts."""
    lines = [f"Headline: {ctx.headline}"]
    if ctx.subheadline:
        lines.append(f"Sub-Headline: {ctx.subheadline}")
    feats = [f for f in (x.strip() for x in ctx.features) if f]
    for i, feat in enumerate(feats):
        lines.append(("Feature: " if i == 0 else "Also feature: ") + feat)
    return "\n\n".join(lines)


def _include_line(ctx: OfferContext) -> str:
    if ctx.logo_mode == "ai":
        return (f"Include The {ctx.client_name} logo and the "
                f"{_brand(ctx)} equipment image.")
    if ctx.logo_mode == "overlay":
        # The logo is still attached as a reference so logo-colour-driven
        # styles see the real palette, but it must not be painted in.
        return (f"Include the {_brand(ctx)} equipment image. The "
                f"{ctx.client_name} logo image is supplied for colour "
                f"reference only — do not paint the logo into the image. "
                f"Keep the bottom-left corner clean — the {ctx.client_name} "
                f"logo will be added there afterwards.")
    return f"Include the {_brand(ctx)} equipment image."


def _dont_lines(ctx: OfferContext) -> str:
    lines = [f"Dont include {d}" for d in
             (s for s in (x.strip().lstrip("-• ") for x in ctx.dont_include) if s)]
    lines.append(f"Dont include the {_brand(ctx)} logo anywhere other "
                 f"than on the HVAC unit itself")
    lines.append("Dont include name of country or city / location")
    return "\n".join(lines)


_FOOTER = (
    "MAKE SURE the image is 1:1 aspect ratio\n"
    "NO Call Today CTA. CTA if you want to include one should just be "
    "\"Click 'Learn More' Below!\"\n"
    "Make sure it's fully in English."
)


def _designed(ctx: OfferContext, opening: str, flavor: str = "") -> str:
    return _assemble(
        opening,
        _offer_lines(ctx),
        _include_line(ctx),
        flavor,
        _dont_lines(ctx),
        _FOOTER,
    )


# ── Shared blocks (organic family — ported from the Claude pipeline spec) ─────

def _organic_reference_line(ctx: OfferContext) -> str:
    """The pipeline originals mandate an explicit match-it-EXACTLY anchor."""
    b = _brand(ctx)
    return (f"The user will supply the {b} HVAC unit reference image "
            f"separately — match it EXACTLY: housing shape, grille louver "
            f"pattern and spacing, color, top fan grille design, {b} badge "
            f"placement and styling, screw and panel details.")


def _organic_logo_line(ctx: OfferContext) -> str:
    if ctx.logo_mode == "ai":
        return (f"The {ctx.client_name} company logo is supplied as a "
                f"separate reference image — composite it small in the "
                f"bottom-left corner of the final image. Keep that corner "
                f"clean and uncluttered.")
    if ctx.logo_mode == "overlay":
        return (f"Keep the bottom-left corner of the image clean and "
                f"uncluttered — the {ctx.client_name} logo will be "
                f"composited there afterwards.")
    return ""


_ORGANIC_FORMAT_LINE = (
    "Format: 1080x1080 square — the image fills the entire square frame "
    "edge to edge. Every piece of rendered text is fully in English with "
    "perfect spelling."
)

# The pipeline original's mandatory closing exclusion list, verbatim items,
# plus the standing brief rules (location / phone / brand-as-text).
_ORGANIC_BASE_EXCLUSIONS = [
    "value stacks", "CTA buttons", "'tap below' prompts", "checkmarks",
    "colored banners or boxes", "marketing overlays", "bold block headlines",
    "watermarks", "ad labels", "the word 'advertisement'",
    "additional branding beyond the user's logo", "text boxes",
    "polished typography", "traditional ad headline treatments",
    "the technician's face or body", "trucks",
    "branded uniforms or patches or embroidery", "stock photography vibes",
    "gradients", "neon colors", "emojis", "drop shadows on text",
    "lens flares",
    "anything that makes the image look like a traditional paid ad",
    "any city, state, region, or location names",
    "phone numbers or website URLs",
    "the HVAC brand name rendered as text anywhere except the badge on the "
    "unit itself",
]


def _exclusion_block(ctx: OfferContext, negations: str = "",
                     allow_location: bool = False) -> str:
    items = list(_ORGANIC_BASE_EXCLUSIONS)
    if allow_location:
        items = [
            ("any city, state, region, or location names other than the "
             "quoted homeowners-only callout line"
             if i.startswith("any city") else i)
            for i in items
        ]
    user_items = [s for s in (d.strip().lstrip("-• ") for d in ctx.dont_include) if s]
    items = user_items + items
    # De-dup while preserving order (case-insensitive).
    seen: set[str] = set()
    deduped = []
    for i in items:
        k = i.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(i)
    block = "Do NOT include: " + "; ".join(deduped) + "."
    if negations:
        block += " " + negations
    return block


# Lighting variants rotated across repeats of the organic close-up style.
# Rain droplets stay tied to overcast light so the scene stays coherent.
_CLOSEUP_LIGHT_VARIANTS = [
    "Lighting: soft overcast diffused light, with fine rain droplets on the "
    "top metal cap running down the painted louvers.",
    "Lighting: bright, clean daylight — the unit dry, with crisp subtle "
    "reflections on the top metal cap.",
    "Lighting: warm late-afternoon golden hour — the unit dry, with soft "
    "warm highlights along the louvers.",
]


def _variant_line(ctx: OfferContext, pool: list[str]) -> str:
    return pool[ctx.variant % len(pool)]


# ── Style definitions ──────────────────────────────────────────────────────────

@dataclass
class Style:
    key: str
    label: str
    family: str          # "designed" | "organic"
    description: str     # short UI blurb
    build: object        # callable(OfferContext) -> str


def _assemble(*blocks: str) -> str:
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())


# — Designed family — the operator's proven prompts, verbatim, slots only ——————

def _bold_offer(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a bold, graphic-heavy HVAC advertisement for "
        f"{ctx.client_name}. Make this feel like a high-performing Facebook "
        f"ad creative with strong typography, big offer callouts, and a "
        f"prominently featured {_brand(ctx)} system. Keep it clean, "
        f"direct-response focused, and easy to understand at a glance.",
        "Make this ad feel more upscale and lifestyle-driven than the others.",
    )


def _offer_badges(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a bold, graphic-heavy HVAC advertisement for "
        f"{ctx.client_name} with a strong offer-first layout. Use large "
        f"readable text, colourful badges, and a prominently featured "
        f"{_brand(ctx)} system. Make it look like a high-performing "
        f"Facebook ad creative with bright, popping colours matching the "
        f"{ctx.client_name} logo.",
        "Keep the design simple, bold, and easy to read fast.",
    )


def _premium_polished(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a premium, polished HVAC advertisement for "
        f"{ctx.client_name}. {_brand(ctx)} system. Make the ad feel "
        f"high-end, clean, and trustworthy while still being very readable "
        f"and conversion-focused.",
        "Make this ad feel visually rich, warm, and different from the others.",
    )


def _luxury_lifestyle(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a premium, upscale HVAC advertisement for "
        f"{ctx.client_name}. Show a {_noun_for(ctx)} with a pool, patio, "
        f"and a polished high-end exterior. Feature the {_brand(ctx)} "
        f"HVAC system clearly but elegantly. Make the ad feel premium, "
        f"aspirational, and clean. Use a brighter, more luxurious palette "
        f"with white and cool blue tones as the base.",
        "Make this ad feel more upscale and lifestyle-driven than the others.",
    )


def _home_install(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a premium, polished HVAC advertisement for "
        f"{ctx.client_name}. Show a beautiful {_noun_for(ctx)} with a "
        f"neatly installed {_brand(ctx)} system at the side of the "
        f"house. Make the ad feel high-end, clean, and trustworthy while "
        f"still being very readable and conversion-focused.",
        "Make this ad feel visually rich, warm, and different from the others.",
    )


def _minimal_editorial(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a clean, modern, minimalist HVAC advertisement for "
        f"{ctx.client_name}. Show a sleek {_noun_for(ctx)} exterior with a "
        f"bright, airy, almost editorial look. Feature the "
        f"{_brand(ctx)} HVAC system clearly and keep the design very "
        f"polished and uncluttered. Use a mostly white and light neutral "
        f"background, with the {ctx.client_name} logo colours used as "
        f"accent colours only, for a very different look from the darker "
        f"or more tropical ads.",
        "Make this ad feel minimal, premium, fresh, and very different "
        "from the other concepts.",
    )


def _vibrant_backyard(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a bright, colourful tropical HVAC advertisement for "
        f"{ctx.client_name}. Show a beautiful backyard with lush "
        f"landscaping, sunshine, and a premium {_noun_for(ctx)} in the "
        f"background. Feature the {_brand(ctx)} HVAC system clearly. "
        f"Make this ad feel more vibrant and tropical than the others. Use "
        f"a flipped colour scheme inspired by the {ctx.client_name} logo: "
        f"the logo's secondary colour as the dominant colour, with its "
        f"primary colour and white as accents instead of the other way "
        f"around.",
        "Make the ad bold, tropical, and visually different from a "
        "standard home exterior ad.",
    )


def _split_season(ctx: OfferContext) -> str:
    return _designed(
        ctx,
        f"Create a bold, creative HVAC advertisement for {ctx.client_name} "
        f"showing a split-season {_noun_for(ctx)} concept. One side of the "
        f"image should feel hot and summery with red/orange tones, and the "
        f"other side should feel cool/wintery with blue icy tones. Feature "
        f"the {_brand(ctx)} system clearly as the all-season solution. "
        f"Match the energetic fire-and-ice style of the {ctx.client_name} "
        f"logo.",
    )


# — Organic family ——————————————————————————————————————————————————————————————

def _note_lines(ctx: OfferContext, max_lines: int = 4) -> list[str]:
    lines = [ctx.headline]
    if ctx.subheadline:
        lines.append(ctx.subheadline)
    lines.extend(ctx.features)
    return [l.strip() for l in lines if l.strip()][:max_lines]


_POV_VARIANTS = [
    "Soft overcast diffused light with fine rain droplets on the top and "
    "grille of the unit.",
    "Warm late-afternoon light with long soft shadows across the concrete "
    "pad.",
    "Bright clear morning light with crisp detail on the unit's grille.",
]


def _pov_bodycam(ctx: OfferContext) -> str:
    note_lines = _note_lines(ctx)
    note = "\n  ".join(_q(l) for l in note_lines)
    caption = _q(ctx.headline)
    if ctx.subheadline:
        caption += f" on the first line and {_q(ctx.subheadline)} on the second line"
    # Features that didn't fit on the torn paper still land verbatim.
    leftover = [f for f in (f.strip() for f in ctx.features)
                if f and f not in note_lines]
    if leftover:
        caption += (f", with a smaller final line reading exactly "
                    f"{_q(' + '.join(leftover))}")
    return _assemble(
        f"Ultra-realistic first-person body cam POV photograph, shot from a "
        f"technician's perspective looking down at a {_brand(ctx)} "
        f"residential AC condenser unit. Only the technician's hands and "
        f"forearms are visible — one hand resting lightly on the top edge "
        f"of the unit, the other holding a small torn scrap of white "
        f"notebook paper up against the side grille. NO face, NO body, NO "
        f"torso. Plain unbranded long-sleeve work shirt in neutral gray "
        f"with zero logos, patches, or stripes. The unit sits on a concrete "
        f"pad at the side of {_scene_for(ctx)}. Realistic install details: "
        f"refrigerant line cover up the wall, electrical disconnect box, "
        f"proper wall clearance. {_variant_line(ctx, _POV_VARIANTS)} "
        f"Shallow depth of field — the unit and torn paper tack-sharp, the "
        f"house softly blurred.",
        f"The torn paper shows casual handwritten ballpoint pen writing "
        f"with uneven baselines — real-person handwriting — listing these "
        f"exact lines stacked one per line:\n  {note}",
        f"Across the top of the image: clean modern medium-bold white "
        f"sans-serif caption text, native Instagram-feed feel — no banner, "
        f"no box, no background tint, no drop shadow. Caption text, "
        f"rendered exactly: {caption}. A small subtle white downward "
        f"chevron sits below the caption pointing at the unit.",
        _organic_reference_line(ctx),
        _organic_logo_line(ctx),
        _ORGANIC_FORMAT_LINE,
        _exclusion_block(ctx, negations="No front yard. No rooftop. "
                                        "No floating unit."),
    )


_RED_TERM_RE = re.compile(
    r"\$[\d][\d,.]*|\d+(?:\.\d+)?\s*%|\d+\s*(?:months?|years?|days?|weeks?)"
    r"|\bfree\b|\b0%\b",
    re.IGNORECASE,
)


def _marker_colors(headline_line: str) -> str:
    """Name which headline words are RED vs BLACK, as the pipeline original
    mandates, instead of leaving the split to the image model."""
    reds = [m.group(0).strip() for m in _RED_TERM_RE.finditer(headline_line)]
    blacks = [s.strip(" +") for s in _RED_TERM_RE.split(headline_line)]
    blacks = [s for s in (b.strip() for b in blacks) if s]
    if not reds:
        return "BLACK marker: the entire headline."
    lines = ["RED: " + ", ".join(_q(r) for r in reds)]
    if blacks:
        lines.append("BLACK: " + ", ".join(_q(b) for b in blacks))
    return "\n".join(lines)


def _ugly_marker(ctx: OfferContext) -> str:
    headline_line = ctx.headline + (f" + {ctx.subheadline}" if ctx.subheadline else "")
    bullets = "\n  ".join(f"- {_q(f)}" for f in ctx.features if f.strip())
    bullets_block = ""
    if bullets:
        bullets_block = (
            f"Below the headline: smaller black-marker lines using dashes "
            f"(not bullets, not checkmarks) listing these exact offer "
            f"details:\n  {bullets}"
        )
    geo = ""
    if ctx.callout:
        geo = (
            f"A tiny black-marker line in parentheses, left-aligned, tucked "
            f"just under the headline, rendered exactly: "
            f"{_q('(' + ctx.callout + ' homeowners only)')}. Smaller than "
            f"the headline, similar size to the bullet lines."
        )
    punchlines = [
        "...our marketer quit",
        "...made by the owner himself",
        "...this is what happens when the marketer quits",
        "...we're not paying anyone to fix this",
    ]
    punch = punchlines[ctx.variant % len(punchlines)]
    return _assemble(
        f"Deliberately ugly, low-effort-looking square image on a "
        f"completely plain pure white background. A {_brand(ctx)} "
        f"residential AC condenser unit sits dead-center on the white "
        f"space — no shadows, no environment, no pad, isolated "
        f"product-shot style. The unit is the only 'real' element; "
        f"everything else is messy handwritten marker scrawled around it, "
        f"self-aware and unpolished.",
        f"Across the top: large messy handwritten marker on 2-3 lines "
        f"reading exactly {_q(headline_line)}. The two marker colors mix "
        f"naturally within the lines:\n{_marker_colors(headline_line)}",
        bullets_block,
        geo,
        f"At the bottom, centered under the unit, spaced-out black "
        f"handwritten marker reading exactly {_q(punch)}. The handwriting "
        f"throughout must look genuinely human — uneven baselines, "
        f"slightly inconsistent letter sizes, real-owner-with-a-Sharpie "
        f"energy. Not designer-styled, not fake-messy.",
        _organic_reference_line(ctx),
        _organic_logo_line(ctx),
        _ORGANIC_FORMAT_LINE,
        _exclusion_block(ctx, allow_location=bool(ctx.callout),
                         negations="No shadows. No environment. "
                                   "No concrete pad. No front yard."),
    )


def _product_closeup(ctx: OfferContext) -> str:
    caption = _q(ctx.headline)
    if ctx.subheadline:
        caption += f" on the first line and {_q(ctx.subheadline)} on the second line"
    if ctx.features:
        extras = " + ".join(f.strip() for f in ctx.features if f.strip())
        caption += (f", with a smaller final line reading exactly "
                    f"{_q(extras)}")
    return _assemble(
        f"Ultra-realistic DSLR-style tight close-up photograph of a "
        f"{_brand(ctx)} residential AC condenser unit, shot at roughly "
        f"a 3/4 angle, framed slightly off-center to showcase the brand "
        f"badge and the texture of the grille louvers. Shallow depth of "
        f"field — badge and front grille tack-sharp, background falling "
        f"into soft creamy bokeh. 85mm prime, f/1.8 feel. The unit sits on "
        f"a concrete pad against the exterior wall of {_scene_for(ctx)}, "
        f"softly blurred behind it. Subtle realistic metal texture, "
        f"visible install details: paintable line cover, electrical "
        f"disconnect box, proper wall clearance.",
        _variant_line(ctx, _CLOSEUP_LIGHT_VARIANTS),
        f"Across the top: clean modern medium-bold white sans-serif "
        f"caption text, native Instagram-feed feel — no banner, no box, "
        f"no background tint, no drop shadow. Caption text, rendered "
        f"exactly: {caption}. A small subtle white downward chevron sits "
        f"below the caption pointing at the unit.",
        _organic_reference_line(ctx),
        _organic_logo_line(ctx),
        _ORGANIC_FORMAT_LINE,
        _exclusion_block(ctx, negations="No front yard. No rooftop. "
                                        "No floating unit."),
    )


# ── Registry ───────────────────────────────────────────────────────────────────

STYLES: dict[str, Style] = {
    s.key: s for s in [
        Style("bold_offer", "Bold Offer Blast", "designed",
              "Graphic-heavy, big typography, offer front and center. "
              "Classic high-performing FB creative.", _bold_offer),
        Style("offer_badges", "Offer-First Badges", "designed",
              "Offer-first layout with large readable text and colourful "
              "badges in the logo's colours.", _offer_badges),
        Style("premium_polished", "Premium Polished", "designed",
              "High-end, clean, and trustworthy while staying readable "
              "and conversion-focused.", _premium_polished),
        Style("luxury_lifestyle", "Luxury Lifestyle", "designed",
              "Upscale aspirational home scene, white and cool-blue "
              "luxury palette.", _luxury_lifestyle),
        Style("home_install", "Neat Install", "designed",
              "Beautiful home with the unit neatly installed at the side "
              "of the house. Warm and real.", _home_install),
        Style("minimal_editorial", "Minimal Editorial", "designed",
              "Bright, airy, mostly white. Logo colors as accents only. "
              "Very polished, very uncluttered.", _minimal_editorial),
        Style("vibrant_backyard", "Vibrant Backyard", "designed",
              "Lush backyard, sunshine, bold color scheme flipped from "
              "the client logo.", _vibrant_backyard),
        Style("split_season", "Fire & Ice", "designed",
              "Split-frame hot/cold seasons — great for heat pumps and "
              "all-season systems.", _split_season),
        Style("pov_bodycam", "POV Body Cam", "organic",
              "First-person tech POV holding a handwritten offer note "
              "against the unit. Feels native, not like an ad.", _pov_bodycam),
        Style("ugly_marker", "Marketer Quit", "organic",
              "Deliberately ugly white background + red/black marker "
              "scrawl. Pattern-interrupt classic.", _ugly_marker),
        Style("product_closeup", "Product Close-Up", "organic",
              "DSLR macro shot of the unit with a clean white caption. "
              "Premium but organic.", _product_closeup),
    ]
}

DEFAULT_STYLE_KEYS = [
    "bold_offer", "premium_polished", "luxury_lifestyle", "home_install",
    "minimal_editorial",
]


def build_prompt(style_key: str, ctx: OfferContext) -> str:
    """Render one style template into a final gpt-image-1 prompt."""
    style = STYLES.get(style_key)
    if style is None:
        raise KeyError(f"Unknown ad style: {style_key!r}")
    return style.build(ctx)
