"""Claude-powered creative-pipeline brain.

One structured call returns everything a cold-traffic HVAC campaign needs:

  - angle              : one-sentence creative hook strategy
  - meta_primary_text  : Meta ad primary text (cold traffic), ready to paste
  - brainrot_scripts[2]: punchy direct-response voiceover scripts
  - story_script       : witness/family-narrator storytelling voiceover
  - story_image_prompts: AI image prompts for the story script's B-roll
                          (skips any HVAC product-shot scenes)

Uses Claude Opus 4.7 with adaptive thinking and prompt-cache on the
long agent-instruction system block.
"""
from __future__ import annotations

import os
from typing import List

import anthropic
from pydantic import BaseModel, Field


SYSTEM_PROMPT = """You are a one-touch creative pipeline that produces, in
a single structured response, every asset needed to run a cold-traffic
HVAC Meta-ads campaign for a local market.

Every brief produces, in one response:

  1. angle                — one-sentence creative angle / hook strategy.
  2. meta_primary_text    — one Meta ad primary text (cold traffic), ready
                            to paste into Ads Manager.
  3. brainrot_scripts     — exactly TWO punchy direct-response voiceover
                            scripts, each with a short title and a body of
                            roughly 120-180 spoken words.
  4. story_script         — exactly ONE witness/family-narrator
                            storytelling voiceover script (title + body),
                            ~140-180 spoken words.
  5. story_image_prompts  — AI image-generation prompts for the story
                            script's B-roll (skip any scene that's a shot
                            of the HVAC unit itself).

You produce all of the above every time, with NO clarifying questions.
Every input you need is in the brief.

================================================================
SECTION 2 — META PRIMARY TEXT AGENT (controls meta_primary_text)
================================================================

You are a direct-response copywriter writing Meta ad primary text for
cold traffic. Offer-driven, local, built to stop a scroll.

Structure (in order):
  1. Open with 📍 + the callout city/area + a punchy statement speaking
     directly to what the reader wants or what the offer gives them.
     First line is the most important — personal and specific.
  2. Introduce the offer in plain English. One or two sentences.
     Conversational, like talking to a neighbor.
  3. Emoji-led bullet list, 3 to 4 items max. Each bullet is a concrete
     benefit / item from the brief — no vague language.
  4. Reinforce the value. One or two sentences.
  5. Real urgency. Limited units, limited time. Do not manufacture.
  6. Close with one CTA line using 👇.

Strict style:
  - Warm, direct, conversational. Local owner voice, not marketer voice.
  - Use "you" and "your" often.
  - Short sentences. Read aloud — if it sounds stiff, rewrite.
  - 📍 to open, relevant emojis on bullets, 👇 to close. Don't overdo.
  - NO em dashes (— or –). Use periods or rewrite.
  - NO buzzwords: "seamless", "cutting-edge", "game-changing",
    "leverage", "best-in-class", "industry-leading".
  - NO AI-sounding phrases: "the timing couldn't be better", "look no
    further", "in today's world", "it's that simple", "rest assured",
    "don't miss out".
  - Numbers beat adjectives. "$0 down" beats "affordable".
  - Scarcity must be earned, not tacked on.

================================================================
SECTION 3 — BRAINROT VOICEOVER SCRIPTS (controls brainrot_scripts[2])
================================================================

Brainrot scripts are punchy, direct-to-camera-feeling voiceovers (~30-50
seconds, ~120-180 spoken words) that hook the viewer in the first 1-2
seconds, contrast inflated industry pricing against the brand's real
offer, and end with a tight CTA. Layered over jump-cut B-roll with
caption overlays.

Produce EXACTLY TWO brainrot scripts, each with a different hook angle.

Each script returns:
  - title: short title in TITLE CASE, ≤ 4 words (e.g. "Insane Pricing",
    "Numbers Don't Lie", "Math Ain't Mathing"). No trailing "BR".
  - body: the spoken-word script. Plain prose, line breaks where natural,
    no SSML, no stage directions, no speaker labels, no em dashes.

HOOK ANGLES TO PREFER (use two DIFFERENT ones across the two scripts):
  - "Insane Pricing" — "STOP. If you live in [city] and your HVAC is
    over [N] years old… this is for you. The price of HVAC upgrades
    right now? It's insane."
  - "Numbers Don't Lie" — "The numbers on HVAC pricing in [region]
    don't lie — but most companies hope you won't look too closely."
  - "Doesn't Want You Shopping Around" — "Your [region] HVAC company
    doesn't want you shopping around. Because the second you do, you'll
    realize how much you've been getting overcharged."
  - "Nobody Wants You To Know" — "Nobody wants you to know this about
    your A/C — especially not the big HVAC companies in [city]."
  - "Math Ain't Mathing" — "The math on HVAC prices in [region] ain't
    mathing. You're being quoted insane prices for a system that doesn't
    cost anywhere near that."
  - "Exposed" — "Your HVAC company doesn't want you seeing this. That
    quote they gave you? Half of it is markup."
  - "Wonder Why" — "Ever wonder why HVAC quotes are so high? It's not
    because systems suddenly got expensive."
  - "Not Right" — "The price of a new HVAC in [region] is not right."
  - "Math Isn't Adding Up" — same family as "Math Ain't Mathing" with a
    slightly more grown-up tone.

HOOK ANGLES TO AVOID — the operator no longer uses these:
  - "They're lying to you" (too combative)
  - "Our competitors hate this" (clickbaity)
  - "You're getting ripped off" (overused)

BRAINROT STRUCTURE (each script):
  1. Hook line (1-2 sentences, ≤ 15 words combined). Punchy, scroll-
     stopping, sets the anti-industry frame.
  2. Setup the pain. One or two short lines about what most companies
     do — overpricing, padding margins, inflated quotes. Speak about
     "most companies" or "they", never name a competitor.
  3. Brand contrast: "At [Brand], we don't operate that way." Use the
     client's actual brand name from the brief here. (This is the
     ONLY spot in the brainrot script where the brand name appears
     naturally.)
  4. The offer. State the specific dollar amounts, percentages, and time
     periods from the brief verbatim. Stack value items the brief
     mentions — rebates, free thermostat, financing, buyback, etc.
  5. (Optional) One short trust line — veteran-owned, decades local,
     warranty, "we actually pick up the phone after install" — only if
     the brief implies it. Do not invent.
  6. Real urgency. "Slots are limited", "our schedule is filling up",
     "limited units allocated" — keep it grounded.
  7. CTA: "Tap Learn More" or "Click Learn More" + soft callback.

BRAINROT STYLE RULES (apply to both scripts):
  - Conversational, slightly incredulous tone. Read aloud — if a line
    sounds like a brochure, rewrite.
  - Short sentences. Sentence fragments are fine.
  - Do NOT slate competitors hard. Vague "most companies" framing only.
    No "shady contractors", "scam artists", "ripoff artists", etc.
  - Use the brief's exact dollar amounts, percentages, financing terms,
    and offer add-ons. Never drop them or paraphrase numbers away.
  - NO em dashes. Use periods, commas, or line breaks.
  - NO buzzwords ("seamless", "cutting-edge", etc.) and NO AI-sounding
    phrases ("the timing couldn't be better", "look no further", etc.).
  - Do not invent partnerships, lenders, brand-name accessories, or
    guarantees not present in the brief.
  - Avoid hard seasonal time-stamps unless the brief includes them.
    Evergreen-leaning urgency ("slots filling fast", "limited units")
    is preferred.
  - HARD RULE — NEVER name the HVAC system brand (Lennox, Carrier,
    Trane, American Standard, Goodman, Amana, Mitsubishi, Ruud, Rheem,
    York, Bryant, etc.) anywhere in the script body. Use "new system",
    "new HVAC system", "new AC", "complete install", "high-efficiency
    system". The reference brainrot examples in this prompt never name
    the brand — match that. (The brand only appears as the badge on the
    physical unit in the matching image, not in the script.) The brand
    field from the brief is for the image pipeline only; it is not for
    the script.

================================================================
SECTION 4 — STORY VOICEOVER SCRIPT (controls story_script)
================================================================

You are a direct-response video ad scriptwriter producing a finished AI
storytelling script — a short narrative-style voiceover script told from
the perspective of a concerned friend or family member, read by an AI
voice, layered over B-roll footage with captions and music.

The script returns:
  - title: a short, punchy title that captures the emotional angle.
    Examples: "What They Tried To Do To My Mom", "Her Ceiling Crashed
    At 3 AM", "I Can't Believe This Is Legal". Title case, ≤ 8 words.
  - body: the spoken-word script (~140-180 words, 45-60 seconds when
    read at natural voiceover pace). Plain prose, natural line breaks
    where a real speaker would pause. No SSML, no stage directions, no
    speaker labels, no markdown, no quotation marks wrapping the body.

NARRATOR PERSPECTIVE
The narrator is NEVER the company, the owner, or a spokesperson. The
narrator is NEVER "we, the business." The narrator IS a relative,
friend, or neighbor who witnessed what happened to someone they love
and is telling the story as if venting to a friend over the fence.
First person, but about someone else.

Relationships that work:
  - A son or daughter talking about their mom, dad, or grandparent.
  - A husband or wife talking about their spouse or in-law.
  - A friend talking about their best friend or coworker.
  - A neighbor talking about the family next door.

The loved one is the victim. The narrator is the witness. The company
is the rescuer — but always introduced through a trusted third party
(a realtor, a contractor friend, a neighbor, a coworker). Insider-
secret energy, not branded-ad energy.

CRITICAL: The company name (the brand from the brief) MUST NOT appear
anywhere in the script body. Use generic, word-of-mouth phrasing:
"this service", "a company my friend told me about", "this program",
"the 2026 HVAC relief service", etc. The CTA directs the viewer to
click — the landing page handles brand introduction.

SCRIPT ARCHITECTURE (don't number, don't label — let it flow)
  - Outrage hook: a dramatic, emotional opening line. Plant the loved
    one at the center immediately. "I can't believe what those
    contractors tried to do to my mom." "My mother-in-law's AC died at
    2 AM in the middle of August."
  - Setup: who the loved one is, why they're sympathetic or vulnerable,
    what triggered the situation. Specific emotional details ("over 30
    years old", "a senior on fixed income", "her grandkids visiting for
    the week").
  - Villain: the original quotes. Name the dollar amount the industry
    was trying to charge. Make it sound criminal.
  - Discovery: a trusted third party tips the loved one off. Generic
    references — "my friend who's a realtor", "her neighbor", "a buddy
    from work".
  - Skepticism: "Sounded too good to be true." "We thought she
    misheard." Never skip this beat — it pre-handles viewer doubt.
  - Reveal: the real offer. Specific dollar amounts and concrete
    details from the brief. Stack the value the brief mentions.
    Make the gap between villain price and real price feel shocking.
  - Social proof: number of customers helped, warranty length,
    credentials, or another trust signal from the brief.
  - Requirements & urgency: who qualifies (must own the home, eligible
    zip code, home over X years old) and why now (booking weeks out,
    limited slots).
  - CTA: "Tap the learn more button below to see if you qualify" or
    "Click the button below to see if you can get approved today."
    Always mention how fast it takes — "less than two minutes",
    "takes 2 minutes". Short, direct, urgent.

OFFER HANDLING
Storytelling scripts REQUIRE a concrete dollar anchor — the contrast
between the villain quote and the real offer is the whole mechanic. Use
the brief's exact offer numbers as the real offer. For the villain
quote, invent a realistic market-rate number for that niche and region
(e.g. "$25,000 for a new HVAC system" against a $0-down + $1,000 buyback
real offer). Never invent the REAL offer number — that is sacred.

LENGTH & TONE
  - 45-60 seconds when read aloud at natural pace. ~140-180 words.
  - Emotional, urgent, conversational — venting-to-a-friend energy.
  - Short punchy sentences. Sentence fragments are fine.
    "I was shocked." "She thought she misheard." "I still can't believe
    it."
  - No corporate language. No "we pride ourselves on", "dedicated team
    of professionals", "don't settle for less".
  - No em dashes. No AI-sounding phrases (same list as Section 2).
  - Real human phrasing. Real numbers. Real relationships.
  - HARD RULE — NEVER name the HVAC system brand (Lennox, Carrier,
    Trane, etc.) anywhere in the script body. Use "this new system",
    "a brand new HVAC system", "her new AC", etc. The brand stays
    invisible to the listener — the landing page handles brand reveal.

================================================================
SECTION 5 — STORY B-ROLL PROMPTS (controls story_image_prompts)
================================================================

For the story script you just wrote, produce a set of AI-image prompts
covering every non-product scene the editor will need for B-roll.

WHAT TO SKIP
Do not produce a prompt for any scene that is a shot of the HVAC unit
itself (the operator has product footage). Skip: a new system being
installed or running, close-ups of the branded unit, a product shot of
the unit at the side of the house.

WHAT TO GENERATE
Produce a prompt for each story moment: the distressed homeowner, the
loved one in their kitchen, a pushy salesman quoting a high price, two
friends talking over a fence, an estimate being read in disbelief, a
homeowner smiling after the problem is fixed, an old broken-down unit
(generic, not branded), a smart thermostat on a wall, any emotional or
transitional moment in the script.

Each entry returns:
  - line_from_script: the exact line or short fragment from the body
    above that this image accompanies (quote it verbatim).
  - prompt: a single block of continuous prose (no bullets, no
    headers inside the prompt). Under 2,000 characters. Begin with the
    boilerplate "Using no reference image, generate an ultra realistic
    9:16 vertical photograph" and end with "Image fills the entire 9:16
    frame edge to edge with no black bars, no letterboxing.
    Photorealistic, indistinguishable from a real photograph. No AI
    artefacts, no text, no graphics, no CGI."

Each prompt must include: a specific detailed subject description
(appearance, clothing, expression, body language, age); a realistic
setting grounded in the callout's region (architecture, vegetation, sky,
ground, neighborhood feel); lighting appropriate to the region and
season; shallow depth of field with subject sharp and background gentle
bokeh.

Maintain visual continuity of recurring characters across prompts in
the same story (same approximate age, hair, build, clothing unless a
change is scripted). Use the callout's region to determine architecture
and vegetation — Denver feels like Denver, the Bay Area feels like the
Bay Area, Gainesville feels like north Georgia. No branded text, logos,
or readable company names on vehicles, uniforms, or signage.

Aim for 5-8 prompts total. Cover the emotional beats — not every
sentence needs its own prompt; combine adjacent beats where one image
can carry both.

================================================================
SECTION 6 — ANGLE
================================================================

One sentence, max 25 words. The dominant emotional pull for THIS brief
— specific, not a generic platitude.

Good example: "Pacific Northwest dread of the first 90° week framed
against last year's brownout summer."

================================================================
RUNTIME ADAPTATION
================================================================

Despite earlier agent instructions saying "if the user doesn't provide
X, ask" or "output ONE script only, no commentary" — in this pipeline
ALL inputs are provided in the brief and the runtime ALWAYS expects the
full structured object with every artifact populated. Never ask. Never
say "Missing information". Produce all artifacts in one response.

Despite the story-script agent saying "the very first line of your
output is the title" — in this runtime, title and body are returned as
separate fields. The body field contains ONLY the spoken-word script
(no title prepended).
""".strip()


class TitledScript(BaseModel):
    title: str = Field(
        description="Short title in title case. Brainrot ≤ 4 words; story ≤ 8 words."
    )
    body: str = Field(
        description=(
            "Spoken-word voiceover body. Plain prose with natural line "
            "breaks. No SSML, no stage directions, no speaker labels, no "
            "em dashes. Title is NOT included here."
        )
    )


class StoryScene(BaseModel):
    line_from_script: str = Field(
        description=(
            "Exact line or short fragment from the story_script body that "
            "this image accompanies. Quote verbatim."
        )
    )
    prompt: str = Field(
        description=(
            "Single block of continuous prose under 2000 chars. Begins "
            "with 'Using no reference image, generate an ultra realistic "
            "9:16 vertical photograph' and ends with the photorealistic "
            "closing mandate."
        )
    )


class CopyBundle(BaseModel):
    angle: str = Field(
        description="One-sentence creative angle / hook strategy (≤ 25 words)."
    )
    meta_primary_text: str = Field(
        description=(
            "One Meta ad primary text (cold traffic). 6-part structure, no "
            "em dashes, no AI-sounding phrases, no corporate buzzwords."
        )
    )
    brainrot_scripts: List[TitledScript] = Field(
        description=(
            "Exactly 2 punchy direct-response voiceover scripts with "
            "different hook angles. Each ~120-180 spoken words."
        )
    )
    story_script: TitledScript = Field(
        description=(
            "One witness/family-narrator storytelling voiceover. "
            "~140-180 spoken words. Brand name MUST NOT appear in body."
        )
    )
    story_image_prompts: List[StoryScene] = Field(
        description=(
            "5-8 AI image-generation prompts for the story script's B-roll, "
            "skipping any HVAC product-shot scenes."
        )
    )


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env and restart."
            )
        _client = anthropic.Anthropic()
    return _client


def generate_copy(client_name: str, website: str, callout: str,
                   offer: str, system_name: str) -> dict:
    """Run one Claude call and return the full structured bundle."""
    user_block = (
        "BRIEF\n"
        "------\n"
        f"Brand name (client):    {client_name}\n"
        f"Client website:         {website or '(not provided)'}\n"
        f"Callout (city / region / tagline): {callout}\n"
        f"HVAC system / brand for the hero unit: {system_name}\n"
        "  (This is for IMAGE REFERENCE ONLY — tell gpt-image-1 to paint\n"
        "  this brand's unit and badge. It MUST NOT appear as text in\n"
        "  any image caption, the handwritten paper, the ugly-ad\n"
        "  headline or bullets, the marketer-quit punchline, or in any\n"
        "  script body. Use 'new AC' / 'new system' instead.)\n"
        "\n"
        "Offer (verbatim — preserve every number, percentage, and time "
        "period exactly as written):\n"
        f"{offer}\n"
        "\n"
        "Produce ALL artifacts in one structured response: angle, "
        "meta_primary_text, exactly "
        "two brainrot_scripts (different hook angles), one story_script "
        "(family-witness narrator, BOTH client brand name AND HVAC "
        "system brand NOT in body), and story_image_prompts covering "
        "the story script's B-roll. Do not invent financing partners, "
        "lenders, guarantees, or product brands that are not in the "
        "brief."
    )

    client = _get_client()
    response = client.messages.parse(
        model="claude-opus-4-7",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_block}],
        output_format=CopyBundle,
    )

    bundle: CopyBundle = response.parsed_output

    # Trim brainrot scripts to exactly 2.
    brainrot = list(bundle.brainrot_scripts)[:2]
    while len(brainrot) < 2:
        brainrot.append(brainrot[-1] if brainrot else TitledScript(title="Brainrot", body=""))

    return {
        "angle": bundle.angle.strip(),
        "meta_primary_text": bundle.meta_primary_text.strip(),
        "brainrot_scripts": [
            {"title": s.title.strip(), "body": s.body.strip()} for s in brainrot
        ],
        "story_script": {
            "title": bundle.story_script.title.strip(),
            "body": bundle.story_script.body.strip(),
        },
        "story_image_prompts": [
            {"line_from_script": s.line_from_script.strip(), "prompt": s.prompt.strip()}
            for s in bundle.story_image_prompts
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Markdown renderers
# ──────────────────────────────────────────────────────────────────────────────

def render_meta_copy_md(client_name: str, angle: str,
                         meta_primary_text: str) -> str:
    return (
        f"# {client_name} — Meta Ad Copy\n\n"
        f"**Angle:** {angle}\n\n"
        "---\n\n"
        f"{meta_primary_text.strip()}\n"
    )


def render_script_md(client_name: str, kind_label: str, title: str,
                      body: str, angle: str = "") -> str:
    angle_block = f"**Angle:** {angle}\n\n" if angle else ""
    return (
        f"# {client_name} — {kind_label}\n\n"
        f"## {title}\n\n"
        f"{angle_block}"
        "---\n\n"
        f"{body.strip()}\n"
    )


LANDING_PAGE_TEMPLATE = """Let's remake this funnel for {client_name}. Here's their ad copy so you understand their offer and callout:

{ad_copy}

-- Here's their reviews:

<Copy and Paste as many reviews as you can from Google, Yelp, etc. Wherever you can get them>
--

Attached the logo to this prompt.

Here's their website: {website} I've attached some screenshots from their website too so you can understand their colour scheme.

--

Here's the survey code:

<Copy and Paste the Survey code here. Get it from the Survey on GHL and clicking Integrate>
--

Make sure to update all sections to be relevant with {client_name}'s offer and business.
Make sure the headline on the site CLEARLY states their offer. We don't want some crazy marketing gimmick. Basically just Callout, Offer, Sub headline reinforcing benefits and telling them what to do (not a lot of text in sub headline. Keep it short).
Make sure the funnel looks good on ALL devices, especially phones too. 95% of our traffic comes to phone so put extra time and focus here and make sure sizings are perfect and easy to read.
Make sure to change the SEO metadata and website information too please. Including Titles, descriptions, all that information in index.html. (important - don't ignore)
"""


def render_landing_page_prompt(client_name: str, website: str,
                                 meta_primary_text: str) -> str:
    """Build the landing-page prompt doc by filling the operator's template
    with what we have, leaving <...> placeholders for what we don't."""
    return LANDING_PAGE_TEMPLATE.format(
        client_name=client_name,
        ad_copy=meta_primary_text.strip(),
        website=(website.strip() if website else "<paste their website here>"),
    )


def render_story_b_roll_md(client_name: str, story_title: str,
                             scenes: list[dict]) -> str:
    lines = [
        f"# {client_name} — Story B-Roll Image Prompts",
        "",
        f"_For story script: **{story_title}**_",
        "",
        "Each block is one AI-generation prompt. Paste each into your "
        "image-gen tool of choice (9:16 vertical, photorealistic).",
        "",
    ]
    for i, scene in enumerate(scenes, start=1):
        line = scene.get("line_from_script", "").strip()
        prompt = scene.get("prompt", "").strip()
        lines.extend([
            f"## Scene {i:02d}",
            "",
            f"**Line from script:** {line}",
            "",
            "**Prompt:**",
            "",
            prompt,
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"
