"""Smoke test: render every style, check invariants (verbatim-prompt edition)."""
import sys
sys.path.insert(0, r"E:\software porject\hvac-marketer-agent")

from agent.ad_templates import STYLES, SETTINGS, OfferContext, build_prompt, DEFAULT_STYLE_KEYS

failures = []

def check(cond, msg):
    if not cond:
        failures.append(msg)

base = dict(
    client_name="TopFlight Air",
    system_name="Lennox System",
    headline="$0 Down On A New HVAC System",
    subheadline="Up to $3,000 in rebates",
    features=["FREE WiFi Smart Thermostat", "10 Year Warranty"],
    dont_include=["thermostat image", "furnace image"],
    callout="Palm Beach County",
)

for key, style in STYLES.items():
    for logo_mode in ("ai", "overlay", "none"):
        for setting in SETTINGS:
            for variant in (0, 1, 2):
                ctx = OfferContext(**base, setting=setting, logo_mode=logo_mode, variant=variant)
                p = build_prompt(key, ctx)
                tag = f"{key}/{logo_mode}/{setting}/v{variant}"
                check("$0 Down On A New HVAC System" in p, f"{tag}: headline not verbatim")
                check("Up to $3,000 in rebates" in p, f"{tag}: subheadline missing")
                check("FREE WiFi Smart Thermostat" in p, f"{tag}: feature missing")
                check("thermostat image" in p, f"{tag}: user dont_include missing")
                check("English" in p, f"{tag}: English rule missing")
                if key != "ugly_marker":
                    check("Palm Beach" not in p, f"{tag}: location leaked into non-marker style")
                if style.family == "designed":
                    check("MAKE SURE the image is 1:1 aspect ratio" in p, f"{tag}: footer missing")
                    check("Dont include name of country or city / location" in p, f"{tag}: location dont-include missing")
                    check("Headline: $0 Down On A New HVAC System" in p, f"{tag}: headline line format wrong")
                    check("Sub-Headline: Up to $3,000 in rebates" in p, f"{tag}: subheadline line format wrong")
                    check("Feature: FREE WiFi Smart Thermostat" in p, f"{tag}: first feature format wrong")
                    check("Also feature: 10 Year Warranty" in p, f"{tag}: second feature format wrong")
                    check("Dont include thermostat image" in p, f"{tag}: dont line format wrong")
                    if logo_mode == "ai":
                        check("Include The TopFlight Air logo and the Lennox equipment image." in p,
                              f"{tag}: include line wrong for ai logo")
                    check("Lennox System system" not in p, f"{tag}: redundant 'System system' phrasing")
                    if logo_mode == "none":
                        check("TopFlight Air logo" not in p or key in ("offer_badges", "minimal_editorial", "vibrant_backyard", "split_season"),
                              f"{tag}: logo mentioned with no logo")
                    # my old verbose additions must be gone
                    check("Hard rules:" not in p, f"{tag}: verbose rules block leaked")
                    check("Do NOT include:" not in p, f"{tag}: exclusion list leaked into designed")
                    check("Reference image" not in p, f"{tag}: reference block leaked into designed")
                    check("Composition:" not in p and "Lighting:" not in p, f"{tag}: variant lines leaked into designed")
                    check("1:1" in p, f"{tag}: aspect ratio rule missing")
                    check("Click 'Learn More' Below!" in p, f"{tag}: CTA rule missing")
                else:
                    check("Do NOT include:" in p, f"{tag}: organic exclusion list missing")
                    check("Format: 1080x1080 square" in p, f"{tag}: organic format line missing")
                    check("match it EXACTLY" in p, f"{tag}: organic reference anchor missing")
                    check("Hard rules:" not in p, f"{tag}: hard-rules block still in organic")
                    check("Click 'Learn More'" not in p, f"{tag}: CTA allowance leaked into organic")
                    if logo_mode == "ai":
                        check("composite it small in the bottom-left corner" in p, f"{tag}: organic ai-logo line missing")
                    if logo_mode == "overlay":
                        check("will be composited there afterwards" in p, f"{tag}: organic overlay line missing")

# ugly marker geo-gate
p = build_prompt("ugly_marker", OfferContext(**base))
check("(Palm Beach County homeowners only)" in p, "ugly_marker: geo-gate missing")
no_callout = dict(base, callout="")
p2 = build_prompt("ugly_marker", OfferContext(**no_callout))
check("homeowners only" not in p2, "ugly_marker: geo-gate present without callout")

# minimal brief
minimal = OfferContext(client_name="A", system_name="Bryant", headline="$99 Tune-Up")
for key in STYLES:
    p = build_prompt(key, minimal)
    check("$99 Tune-Up" in p, f"{key}: minimal headline missing")
    check("Sub-Headline" not in p, f"{key}: sub-headline present with empty subheadline")
    check("(none)" not in p, f"{key}: literal (none) leaked")

# vary setting rotates PER IMAGE (image_index), not per style repeat
s0 = build_prompt("home_install", OfferContext(**base, setting="vary", variant=0, image_index=0))
s1 = build_prompt("home_install", OfferContext(**base, setting="vary", variant=0, image_index=1))
check(s0 != s1, "home_install: vary setting does not rotate by image index")
check("modern suburban home" in s0, "home_install: setting noun missing")
check("luxury home" in s1, "home_install: second image should get luxury setting")
# same style repeated when styles count is a multiple of rotation length still varies
r0 = build_prompt("home_install", OfferContext(**base, setting="vary", variant=0, image_index=0))
r1 = build_prompt("home_install", OfferContext(**base, setting="vary", variant=1, image_index=5))
check(r0 != r1, "home_install: repeat at index 5 aliases to same setting")

# marker RED/BLACK naming is explicit
p = build_prompt("ugly_marker", OfferContext(**base))
check("RED: " in p and "BLACK: " in p, "ugly_marker: RED/BLACK naming missing")
check('"$0 Down"' in p or '"$0"' in p, "ugly_marker: red term not quoted")
p = build_prompt("ugly_marker", OfferContext(client_name="A", system_name="T", headline="Best Service In Town"))
check("BLACK marker: the entire headline." in p, "ugly_marker: no-red fallback missing")

# closeup: rain only with overcast variant
c0 = build_prompt("product_closeup", OfferContext(**base, variant=0))
c1 = build_prompt("product_closeup", OfferContext(**base, variant=1))
check("rain droplets" in c0 and "overcast" in c0, "closeup v0: overcast+rain pairing broken")
check("rain droplets" not in c1, "closeup v1: rain leaked into dry lighting")

print(f"styles: {len(STYLES)}, defaults: {DEFAULT_STYLE_KEYS}")
if failures:
    print(f"\nFAILURES ({len(failures)}):")
    for f in failures[:40]:
        print(" -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")

print("\n--- SAMPLE: bold_offer (ai logo) ---\n")
print(build_prompt("bold_offer", OfferContext(**base, logo_mode="ai")))
print("\n--- SAMPLE: luxury_lifestyle (beach) ---\n")
print(build_prompt("luxury_lifestyle", OfferContext(**base, setting="beach", logo_mode="ai")))
