"""Northwend (formerly Waypoint, and Portfolio Tracker before that) - single-page Streamlit dashboard.

Run it:  streamlit run dashboard.py   (or double-click dashboard.cmd)
"""

import functools
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

import codefresh

# After a deploy, drop any of our modules still loaded at an older version so
# the imports below load one current set (see codefresh.py).
_OLD_MODULES = codefresh.drop_stale(os.path.dirname(os.path.abspath(__file__)))

import accounts
import advising
import ai_usage
import alerts
import asset_classes
import auth
import charts
import csv_import
import disclosures
import friendly_errors
import fund_holdings
import hosting
import income
import learn
import live_prices
import mailer
import manual_entry
import paste_parse
import screenshot_read
import sample_data
import metrics as M
import news
import perf
import pgcompat
import plans
import prefs
import route
import watchlist
from allocation import CONCENTRATION_PCT, allocate
from portfolio import (SAMPLE_SOURCE, DBError, connect, delete_holdings, snapshot_source,
                       temp_upload, upload_label)
from update_prices import ENV_PATH, latest_snapshot, load_env, refresh_prices, resolve_key
import changes

codefresh.carry_over(_OLD_MODULES)
codefresh.mark_loaded(os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
# Defaults to ./portfolio.db; set PORTFOLIO_DB to point at another file (handy for
# trying the importer against a throwaway copy).
DB = os.environ.get("PORTFOLIO_DB") or os.path.join(HERE, "portfolio.db")
# The staging app (its own Streamlit Cloud app on the `staging` branch, with
# its own database) sets NORTHWEND_ENV = "staging" in its Secrets: every page
# then says so, so it is never mistaken for the live app.
STAGING = (os.environ.get("NORTHWEND_ENV") or "").strip().lower() == "staging"


def _view(name):
    """Run views/<name>.py here, in this script's own namespace - exactly as if
    its code were written at this spot. Each page's code lives in its own file
    so it can be read and changed on its own; nothing else about it changes."""
    path = os.path.join(HERE, "views", f"{name}.py")
    with open(path, encoding="utf-8") as fh:
        exec(compile(fh.read(), path, "exec"), globals())  # noqa: S102

# An unexpected error shows "something went wrong" instead of a traceback; the
# traceback goes to the log. Details show on screen only for a local run. The
# hosted copies (live, staging) also email the admin about it (error_alerts.py,
# once an hour per kind); every copy lists it on Admin > System.
friendly_errors.install(show_details=not pgcompat.is_postgres_dsn(DB)
                        and st.get_option("client.showErrorDetails") in ("full", True, "true"),
                        alert_db=DB, copy="Staging" if STAGING else "Live",
                        send_alerts=pgcompat.is_postgres_dsn(DB))

# said wherever people decide what to share (import, hand entry, paste)
TRUST_LINE = ("We never ask for your brokerage login. Only symbols, share counts and cost "
              "are saved - never balances or full account numbers.")
NOT_KEPT = ("Not kept: the file, image or pasted text itself, balances and gains, and account "
            "numbers beyond their last 3 digits.")


def learn_more(topic):
    """A small, quiet "Learn more" link next to an idea the page explains, to
    a public education page (Investor.gov, FINRA, the CFPB - learn.LEARN_MORE).
    Opens in a new tab."""
    line = learn.learn_more_md(topic)
    if line:
        st.caption(line)

# The brand: Northwend, and the AI guide (the AI Assistant) carries the same
# name - "Ask Northwend". Was Waypoint / Sage until October 2026. Pages keep
# their internal names (session state, links, `if PAGE == ...`); PAGE_LABELS
# is only what people see.
APP_NAME = "Northwend"
TAGLINE = "Your guide from first step to goal."
GUIDE = APP_NAME  # the AI guide shares the app's name: "Ask Northwend"
APP_ICON = ":material/flag:"
PAGE_LABELS = {"AI Assistant": f"Ask {GUIDE}", "Clients": "Your clients"}


def _label(page):
    return PAGE_LABELS.get(page, page)


SAGE_AVATAR = ":material/explore:"   # a compass, for the guide's chat messages


def _avatar(role):
    return SAGE_AVATAR if role == "assistant" else None


LIVE_EVERY_SEC = 60  # how often an open page checks for new prices (live_prices.py)


def _dialog_closed():
    """A dialog's X or Escape: live prices may redraw the page again (they
    wait while a dialog is open, since a redraw would close it)."""
    st.session_state["dialog_open"] = False


# up / down text colors with AA contrast on each theme (as --pt-up / --pt-down)
SIGN_COLORS = {"light": ("#15803d", "#b91c1c"), "dark": ("#4ade80", "#f87171")}

st.set_page_config(page_title=APP_NAME + (" (staging)" if STAGING else ""),
                   page_icon=APP_ICON, layout="wide",
                   initial_sidebar_state="auto")

# App-wide styles: hide Streamlit's own running/deploy widgets, tighten the
# page on phones, and the classes used by the hero, stat tiles, and
# allocation bars below. Text inherits the theme's colors; only marks and
# gain/loss figures carry their own.
st.html("""<style>
/* The Northwend design system's colors for the app's own pieces, per theme
   (the rest is in .streamlit/config.toml); ui_enhancements.js marks the theme
   on the page root. up / down / warning pass WCAG AA (4.5:1) as text; compass
   is the brand blue for bars and marks; line a hairline; line-strong the edge
   of a control (3:1); sunken the track behind a bar; ink-muted quieter
   text that still passes AA. */
:root { --pt-up: #15803d; --pt-down: #b91c1c; --pt-warn: #a16207; --pt-compass: #2a78d6;
  --pt-line: #d5dde5; --pt-line-strong: #74838f; --pt-sunken: #e8eef4; --pt-dawn-soft: #fbebc9;
  --pt-link: #1d5fae; --pt-compass-soft: #e3eefb; --pt-dawn: #f0b23c; --pt-ink-muted: #4d5d6c; }
:root[data-pt-theme="dark"] { --pt-up: #4ade80; --pt-down: #f87171; --pt-warn: #fbbf24;
  --pt-compass: #3987e5; --pt-line: #2a3847; --pt-line-strong: #62748a; --pt-sunken: #1c2a38;
  --pt-dawn-soft: #3a2f17; --pt-link: #7cb3f2; --pt-compass-soft: #16304d; --pt-dawn: #f2bd57;
  --pt-ink-muted: #9eadbb; }
/* the advisor app's role chip, and the bar shown while inside a client's account */
.pt-role { color: var(--pt-link); background: var(--pt-compass-soft); border-color: transparent;
  margin: -.4rem 0 .4rem; }
.st-key-pt_viewing { background: var(--pt-compass-soft); border-color: var(--pt-compass) !important; }
/* Your route (the investor home, route.py): waypoint dots heading to the goal */
.pt-route-label { font-size: .75rem; font-weight: 600; letter-spacing: .02em; opacity: .75;
  margin-bottom: .35rem; }
.pt-route { display: flex; align-items: center; margin: .9rem 0 .2rem; }
.pt-dot { width: 12px; height: 12px; border-radius: 999px; border: 2px solid var(--pt-line-strong);
  flex: none; box-sizing: border-box; }
.pt-dot-done { background: var(--pt-compass); border-color: var(--pt-compass); }
.pt-dot-here { width: 20px; height: 20px; background: var(--pt-compass-soft); border: 4px solid var(--pt-compass); }
.pt-dot-goal, .pt-dot-goal_reached { width: 20px; height: 20px; border: 2px solid currentColor; }
.pt-dot-goal_reached { background: var(--pt-dawn); }
.pt-leg { height: 2px; flex: 1 1 0; max-width: 56px; min-width: 10px; background: var(--pt-line-strong); }
.pt-leg-done { background: var(--pt-compass); }
.st-key-pt_route_reached { background: var(--pt-dawn-soft); border-color: var(--pt-dawn) !important; }
/* Find your direction (Get started): the investor type */
.st-key-pt_direction { border-color: var(--pt-compass) !important; }
.pt-type-name { font-family: Newsreader, Georgia, serif; font-size: 1.75rem; line-height: 1.2;
  font-weight: 500; }
.pt-type-line { opacity: .75; margin-top: .15rem; }
/* the staging app's banner (STAGING): text in the theme's own color */
.pt-staging { background: var(--pt-dawn-soft); border: 1px solid var(--pt-warn); border-radius: .5rem;
  padding: .5rem .9rem; font-size: .9rem; font-weight: 600; }
/* Newsreader is for page and section titles only; card headings (h4 and
   smaller) stay in the text face, Figtree */
h4, h5, h6 { font-family: Figtree, "Segoe UI", system-ui, sans-serif !important; }
/* Controls people must find get the stronger edge (3:1); the theme's
   borderColor stays the hairline for cards and expanders. Focused fields and
   selected pills keep Streamlit's own primary-colored edge. */
[data-testid="stTextInputRootElement"]:not(:focus-within),
[data-testid="stTextAreaRootElement"]:not(:focus-within),
[data-testid="stNumberInputContainer"]:not(:focus-within),
[data-testid="stDateInputField"]:not(:focus-within),
[data-testid="stSelectbox"] [data-baseweb="select"] > div:not(:focus-within),
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:not(:focus-within),
[data-testid^="stBaseButton-secondary"]:not(:hover):not(:focus-visible),
[data-testid="stButtonGroup"] button[aria-checked="false"]:not(:hover):not(:focus-visible) {
  border-color: var(--pt-line-strong) !important; }
/* dark: the selected tab, segment or pill, and a slider's value, are labelled
   in the link blue - the theme's primary blue reads only 3.9:1 as text on the
   dark page */
:root[data-pt-theme="dark"] [data-testid="stButtonGroup"] button[aria-checked="true"],
:root[data-pt-theme="dark"] [data-testid="stTab"][aria-selected="true"],
:root[data-pt-theme="dark"] [data-testid="stSliderThumbValue"] {
  color: var(--pt-link); }
/* captions: Streamlit fades the whole caption to 60%, which left its text
   under AA on the light theme (4.0:1) and a link in it (the Learn more lines)
   too faint on the dark one (3.8:1); fade only the text, a little less, so
   it passes on both and a link keeps its full color */
[data-testid="stCaptionContainer"] { opacity: 1;
  color: color-mix(in srgb, currentColor 70%, transparent); }
/* a slider's min and max labels: Streamlit fades them to 60%, under AA on
   the light theme (4.0:1); the design system's muted text passes on both */
[data-testid="stSlider"]:not(:has([role="slider"][aria-disabled="true"])) [data-testid="stSliderTickBar"] {
  color: var(--pt-ink-muted); }
/* the expedition (ROADMAP T1): faint contour lines behind every page, drawn in
   static/topo-light.svg and topo-dark.svg, one step above the page colour */
[data-testid="stMain"] { background-repeat: no-repeat;
  background-position: right -220px top -40px; background-size: 1400px auto; }
/* movement (ROADMAP T2; ui_enhancements.js adds the classes): a new page fades
   in (opacity only - a transform would unpin the phone tab bar for a moment),
   and the trail draws itself forward when a waypoint is reached */
.pt-page-enter { animation: pt-page-in .35s ease-out; }
@keyframes pt-page-in { from { opacity: 0; } to { opacity: 1; } }
.pt-trail-advance { animation: pt-trail-draw .9s ease-out; }
@keyframes pt-trail-draw { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }
/* milestones and gear (views/kit.py, gear.py): icon tiles, earned ones in dawn */
.pt-gear-row { display: flex; flex-wrap: wrap; gap: 10px 8px; margin: .6rem 0 .45rem !important;
  padding: 0 !important; list-style: none; }
.pt-gear-cell { width: 64px; margin: 0 !important; padding: 0; display: flex; flex-direction: column;
  align-items: center; gap: 4px; }
.pt-gear-label { font-size: .72rem; line-height: 1.2; text-align: center; color: var(--pt-ink-muted); }
.pt-gear-tile { width: 40px; height: 40px; border-radius: 10px; display: inline-flex; flex: none;
  align-items: center; justify-content: center; border: 1px dashed var(--pt-line-strong); }
.pt-gear-earned { border: 0; background: var(--pt-dawn-soft); }
/* the kit window: one row per piece - what it's for, how it's earned */
.pt-gear-item { display: flex; gap: .8rem; align-items: flex-start; }
.pt-gear-head { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .5rem;
  margin-bottom: .15rem; }
.pt-gear-how { font-size: .85rem; color: var(--pt-ink-muted); margin-top: .15rem; }
.pt-gear-why { color: var(--pt-ink-muted); }
.pt-gear-chip { display: inline-block; padding: .05rem .55rem; border-radius: 999px;
  font-size: .75rem; font-weight: 600; color: var(--pt-ink-muted);
  border: 1px dashed var(--pt-line-strong); }
.pt-gear-chip-earned { color: inherit; background: var(--pt-dawn-soft); border: 1px solid var(--pt-dawn); }
.pt-milestone { display: flex; flex-direction: column; gap: .5rem; align-items: flex-start; }
.pt-milestone-badge { width: 76px; height: 76px; border-radius: 999px; background: var(--pt-dawn-soft);
  display: flex; align-items: center; justify-content: center; margin-bottom: .3rem; }
.pt-milestone-title { font-family: Newsreader, Georgia, serif; font-size: 1.6rem; line-height: 1.2;
  font-weight: 500; }
/* storms (views/kit.py render_weather): a calm note, a cool rain edge */
.st-key-pt_storm { border-left: 3px solid var(--pt-compass) !important; }
.pt-storm-title { font-family: Newsreader, Georgia, serif; font-size: 1.3rem; font-weight: 500;
  margin: .1rem 0 .3rem; }
.pt-storm-table { width: 100%; border-collapse: collapse; font-size: .92rem; }
.pt-storm-table th { text-align: left; font-weight: 600; opacity: .75; padding: .3rem .4rem; }
.pt-storm-table td { padding: .35rem .4rem; border-top: 1px solid var(--pt-line); }
/* the trail is two images, one per theme; show the one that matches */
:root[data-pt-theme="dark"] .pt-on-light, :root:not([data-pt-theme="dark"]) .pt-on-dark {
  display: none; }
/* the route as a trail (route.trail_html), and the map plate above a title */
.pt-trail { display: block; width: 100%; max-width: 760px; height: auto; margin: .6rem 0 .1rem; }
.pt-region { font-size: .85rem; opacity: .8; margin: 0 0 .2rem; }
.pt-region b { font-family: Newsreader, Georgia, serif; font-style: italic; font-weight: 500;
  font-size: 1rem; opacity: 1; }
.pt-eyebrow { font-family: Newsreader, Georgia, serif; font-style: italic; font-size: 1rem;
  opacity: .78; margin: 0 0 -1.1rem; }
/* first steps (views/first_steps.py): progress dots, and each screen slides in */
.pt-fs-dots { display: flex; gap: 6px; margin: 0 0 .25rem; }
.pt-fs-dot { flex: 1 1 0; height: 5px; border-radius: 3px; background: var(--pt-sunken);
  transition: background .3s ease; }
.pt-fs-dot-on { background: var(--pt-compass); }
.pt-fs-dot-at { outline: 2px solid var(--pt-compass); outline-offset: 1px; }
/* Learn (views/get_started.py): how far along the route, one segment per
   waypoint - complete ones filled, the open one outlined - and each
   waypoint's one line about why it matters */
.pt-steps { margin: 0 0 .4rem; }
.pt-steps-top { display: flex; justify-content: space-between; align-items: baseline;
  gap: .5rem; font-size: .95rem; margin-bottom: .35rem; }
.pt-steps-top span { font-size: .85rem; color: var(--pt-ink-muted); font-variant-numeric: tabular-nums; }
.pt-steps-bar { display: flex; gap: 4px; }
.pt-steps-seg { flex: 1 1 0; height: 10px; border-radius: 5px; background: var(--pt-sunken);
  box-shadow: inset 0 0 0 1px var(--pt-line); transition: background .3s ease; }
.pt-steps-done { background: var(--pt-compass); box-shadow: none; }
.pt-steps-at { outline: 2px solid var(--pt-compass); outline-offset: 2px; }
.pt-why { font-family: Newsreader, Georgia, serif; font-style: italic; font-size: 1.08rem;
  line-height: 1.4; margin: -.35rem 0 .35rem; }
.st-key-pt_gs_nav button p { font-size: .9rem; }
[class*="st-key-pt_slide_"] { animation: pt-slide-in .35s ease-out both; }
/* a chosen answer in a slide (first steps, Learn's waypoints) reads as chosen
   at a glance: a check, a firmer edge and bold, not just a pale tint */
[class*="st-key-pt_slide_"] button[data-variant="pills"][data-selected="true"] {
  border: 2px solid var(--pt-compass) !important; background: var(--pt-compass-soft) !important;
  color: var(--pt-link) !important; font-weight: 600; }
[class*="st-key-pt_slide_"] button[data-variant="pills"][data-selected="true"]::before {
  content: "\\2713"; margin-right: .35rem; font-weight: 700; }
/* just signed up: the one line about the email link, its Send it again a
   small link-sized button rather than a full row of its own */
.st-key-pt_email_brief { row-gap: 0 !important; }
.st-key-pt_email_brief button { min-height: 0; padding: 0; }
.st-key-pt_email_brief button p { font-size: .85rem; }
/* phones: first steps' Back / Next stay in reach at the bottom of the screen
   (the tab bar steps aside while they're open - render_first_steps) */
@media (max-width: 640px) {
  /* the row sits in a wrapper just its own size, so the wrapper is what sticks */
  div:has(> .st-key-pt_fs_nav) { position: sticky; bottom: 0; z-index: 20; }
  .st-key-pt_fs_nav { background: var(--pt-bg, transparent);
    padding: .5rem 0 calc(.5rem + env(safe-area-inset-bottom)); border-top: 1px solid var(--pt-line); }
}
@keyframes pt-slide-in { from { opacity: 0; transform: translateX(18px); }
  to { opacity: 1; transform: none; } }
/* read by screen readers, not shown */
.pt-sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important;
    transition-duration: .01ms !important; } }
[data-testid="stStatusWidget"], [data-testid="stAppDeployButton"], .stAppDeployButton {
  display: none !important; }
/* room for the top bar (pinned, 3.75rem) above the page */
[data-testid="stMainBlockContainer"] { padding-top: 5.75rem; }
/* the pinned bars, and a script with nothing to show (ui_enhancements.js,
   the sign-in cookie), take no room in the page's own column */
[data-testid="stLayoutWrapper"]:has(> .st-key-pt_topbar),
[data-testid="stLayoutWrapper"]:has(> .st-key-pt_tabbar) { display: contents; }
[data-testid="stElementContainer"]:has(> [data-testid="stHtml"] > script:only-child) {
  display: none; }
/* a slider's end label can poke past a phone's edge; never scroll sideways */
[data-testid="stMain"] { overflow-x: hidden; }
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 4.75rem 1rem 6rem; }
  h1 { font-size: 1.6rem !important; }
  /* a ticker's stats (_stat_tiles): two per line, not one long column */
  .st-key-pt_stat_tiles [data-testid="stColumn"] { min-width: calc(50% - 8px) !important; }
  .st-key-pt_stat_tiles [data-testid="stColumn"]:not(:has([data-testid="stElementContainer"])) {
    display: none; }
  /* tabs (Plan's) wrap onto a second line instead of scrolling sideways
     behind Streamlit's arrow */
  [data-testid="stTabs"] [role="tablist"] { flex-wrap: wrap; overflow-x: visible; row-gap: .25rem; }
  [data-testid="stTabsScrollLeft"], [data-testid="stTabsScrollRight"] { display: none !important; }
}
/* the top bar (_render_top_bar): pinned along the top of the window, on the
   page's own background (--pt-bg, kept in step with light/dark by
   ui_enhancements.js). Streamlit's header strip stays above it, see-through,
   so its three-dot menu sits at the bar's right end. */
[data-testid="stHeader"] { background: transparent; }
[data-testid="stHeader"], [data-testid="stHeader"] * { pointer-events: none; }
[data-testid="stHeader"] [data-testid="stMainMenu"],
[data-testid="stHeader"] [data-testid="stMainMenu"] * { pointer-events: auto; }
.st-key-pt_topbar { position: fixed; top: 0; left: 0; right: 0; z-index: 999980;
  height: 3.75rem; box-sizing: border-box; padding: 0 3.5rem 0 1rem;
  flex-wrap: nowrap !important; gap: .25rem !important;
  overflow-x: auto; overflow-y: hidden; scrollbar-width: none;
  background: var(--pt-bg, #f5f7f9); border-bottom: 1px solid var(--pt-line); }
.st-key-pt_topbar > div { flex: none; width: auto; }
/* lined up with the page's own column once it has its wide margins */
@media (min-width: 864px) { .st-key-pt_topbar { padding: 0 5rem; } }
.st-key-pt_topbar [data-testid="stPopoverButton"] > div { gap: .3rem; }
.pt-brand { display: flex; align-items: center; gap: .35rem; margin-right: 1rem;
  white-space: nowrap; font-family: Newsreader, Georgia, serif; font-size: 1.35rem;
  font-weight: 500; line-height: 1; }
.pt-brand-mark { font-family: "Material Symbols Rounded"; font-size: 1.5rem; font-weight: 400;
  color: var(--pt-compass); font-feature-settings: "liga"; }
/* + Add holdings and the name menu keep to the right end */
.st-key-pt_topbar > .st-key-pt_add,
.st-key-pt_topbar > :not(.st-key-pt_add) + .st-key-pt_me { margin-left: auto; }
/* their words in full (Streamlit would cut a menu button's words short) */
.st-key-pt_topbar [data-testid="stPopoverButton"] [data-testid="stMarkdownContainer"],
.st-key-pt_client_login [data-testid="stMarkdownContainer"] { min-width: max-content; }
/* the client's own Get started, in the viewing bar: marked as the tabs are */
.st-key-viewing_start button[kind="primary"] { background: var(--pt-compass-soft) !important;
  color: var(--pt-link) !important; border-color: var(--pt-compass) !important; }
/* the tabs: plain words; the page showing reads in the link blue on a soft
   compass tint, bold (and aria-current, ui_enhancements.js) */
.st-key-pt_topbar [class*="st-key-nav_"] button { min-height: 2.25rem; padding: .3rem .75rem;
  border: 0; border-radius: .5rem; font-weight: 500; }
.st-key-pt_topbar [class*="st-key-nav_"] button:hover { background: var(--pt-sunken); }
.st-key-pt_topbar [class*="st-key-nav_"] button[kind="primary"],
.st-key-pt_tabbar button[kind="primary"] {
  background: var(--pt-compass-soft) !important; color: var(--pt-link) !important;
  border-color: transparent !important; }
.st-key-pt_topbar [class*="st-key-nav_"] button[kind="primary"] p,
.st-key-pt_tabbar button[kind="primary"] p { font-weight: 600; }
/* an advisor's Viewing: says what it is, the account's name after it */
.st-key-viewing_select [role="group"] { background: transparent; }
.st-key-viewing_select [role="group"]::before { content: "Viewing"; align-self: center;
  padding-left: .7rem; font-size: .8rem; color: var(--pt-ink-muted); white-space: nowrap; }
.st-key-viewing_select [role="combobox"] { background: transparent; padding-left: .4rem;
  font-weight: 600; }
/* inside + Add holdings and the name menu: a list of rows, left-aligned and
   close together; the page showing marked as in the bar */
[data-testid="stPopoverBody"]:has(.st-key-menu_logout) [data-testid="stVerticalBlock"],
[data-testid="stPopoverBody"]:has(.st-key-add_manual) [data-testid="stVerticalBlock"] {
  gap: .3rem; }
[data-testid="stPopoverBody"]:has(.st-key-menu_logout) button,
[data-testid="stPopoverBody"]:has(.st-key-add_manual) button { padding: .45rem .6rem; }
[data-testid="stPopoverBody"]:has(.st-key-menu_logout) button > div,
[data-testid="stPopoverBody"]:has(.st-key-add_manual) button > div {
  justify-content: flex-start; text-align: left; }
[data-testid="stPopoverBody"]:has(.st-key-menu_logout) button:hover,
[data-testid="stPopoverBody"]:has(.st-key-add_manual) button:hover {
  background: var(--pt-sunken); }
[class*="st-key-menu_"] button[kind="primary"] { background: var(--pt-compass-soft) !important;
  color: var(--pt-link) !important; border-color: transparent !important; }
[class*="st-key-menu_"] button[kind="primary"] p { font-weight: 600; }
/* an open menu's button keeps readable words (Streamlit dims a plain one
   to a dark blue, hard to read on the dark theme) */
.st-key-pt_me [data-testid="stPopoverButton"][aria-expanded="true"],
.st-key-pt_client_login [data-testid="stPopoverButton"][aria-expanded="true"] {
  color: var(--pt-link); }
/* the name menu: a long name or email shortens with "..." */
.st-key-pt_me button { max-width: 15rem; }
.st-key-pt_topbar .st-key-pt_me button [data-testid="stMarkdownContainer"] {
  width: max-content; min-width: 0; max-width: 10rem; flex-shrink: 0; overflow: hidden; }
.st-key-pt_me button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Money's tabs (Income, Activity, Watchlist; _money_tabs): words on a
   hairline, the one showing underlined in the compass blue */
.st-key-pt_money_tabs { gap: 1.5rem !important; border-bottom: 1px solid var(--pt-line);
  margin: -.5rem 0 .25rem; }
.st-key-pt_money_tabs button { min-height: 2.5rem; padding: .25rem .1rem; border: 0;
  border-bottom: 2px solid transparent; border-radius: 0; background: transparent !important;
  margin-bottom: -1px; }
.st-key-pt_money_tabs button:hover { color: var(--pt-link); }
.st-key-pt_money_tabs button[kind="primary"] { color: var(--pt-link) !important;
  border-bottom-color: var(--pt-compass) !important; }
.st-key-pt_money_tabs button[kind="primary"] p { font-weight: 600; }
/* a narrower window: the brand's name steps aside first (sooner for an
   advisor, whose bar holds more), then the two menus' words; past that the
   bar scrolls sideways rather than cut anything off */
@media (min-width: 641px) and (max-width: 1000px) {
  .pt-brand-name { display: none; }
  .pt-brand { margin-right: .25rem; }
}
@media (min-width: 641px) and (max-width: 1250px) {
  .pt-brand-compact .pt-brand-name { display: none; }
  .pt-brand-compact { margin-right: .25rem; }
}
@media (min-width: 641px) and (max-width: 1100px) {
  .st-key-pt_topbar:has(.pt-brand-compact) .st-key-pt_add button [data-testid="stMarkdownContainer"],
  .st-key-pt_topbar:has(.pt-brand-compact) .st-key-pt_me button [data-testid="stMarkdownContainer"] {
    display: none; }
}
@media (min-width: 641px) and (max-width: 760px) {
  .st-key-pt_topbar:has(.pt-brand-compact) [class*="st-key-nav_"] button { padding: .3rem .4rem; }
}
@media (min-width: 641px) and (max-width: 900px) {
  .st-key-pt_topbar .st-key-pt_add button [data-testid="stMarkdownContainer"],
  .st-key-pt_topbar .st-key-pt_me button [data-testid="stMarkdownContainer"] { display: none; }
  .st-key-pt_topbar [class*="st-key-nav_"] button { padding: .3rem .5rem; }
  .st-key-pt_topbar [data-testid="stSelectbox"] { width: 9.5rem !important; }
}
/* phone tab bar (_render_tab_bar): pinned to the bottom on narrow screens,
   hidden on wider ones where the top bar holds the tabs. */
.st-key-pt_tabbar { display: none !important; }
/* the sign-up form's hidden field (_signup): people never see it, bots fill it in */
.st-key-signup_website { display: none !important; }
@media (max-width: 640px) {
  .st-key-pt_tabbar {
    display: flex !important; position: fixed; left: 0; right: 0; bottom: 0; z-index: 999990;
    justify-content: space-around; gap: 0 !important;
    padding: .3rem .25rem calc(.3rem + env(safe-area-inset-bottom));
    background: var(--pt-bg, #0d1620); border-top: 1px solid var(--pt-line);
  }
  .st-key-pt_tabbar > div { flex: 1 1 0; min-width: 0; }
  .st-key-pt_tabbar button {
    width: 100%; min-height: 3.1rem; padding: .15rem .1rem; border: none;
  }
  .st-key-pt_tabbar button p { font-size: .7rem; line-height: 1.15; text-align: center; }
  /* the icon on its own line, above the label */
  .st-key-pt_tabbar button p span[role="img"] {
    display: block !important; font-size: 1.4rem; line-height: 1.2; margin: 0 auto;
  }
  /* the top bar slims to the brand, + and the name's icon: the tabs are
     along the bottom */
  .st-key-pt_topbar { height: 3.25rem; padding: 0 3rem 0 1rem; }
  .st-key-pt_topbar [class*="st-key-nav_"] { display: none !important; }
  .st-key-pt_add button [data-testid="stMarkdownContainer"],
  .st-key-pt_me button [data-testid="stMarkdownContainer"] { display: none; }
  .st-key-pt_add button, .st-key-pt_me button { min-width: 2.75rem; padding: .3rem .55rem; }
  .pt-brand-compact .pt-brand-name { display: none; }
  .st-key-pt_topbar [data-testid="stSelectbox"] { width: 9.5rem !important; }
  .st-key-viewing_select [role="group"]::before { content: none; }
}
.pt-status { font-size: .8rem; opacity: .75; margin-top: -.6rem; }
.pt-hero-label { font-size: .85rem; opacity: .7; }
.pt-hero-value { font-size: 2.6rem; font-weight: 700; line-height: 1.15;
  font-variant-numeric: tabular-nums; }
.pt-hero-delta { font-size: 1rem; font-weight: 600; margin-top: .15rem; }
.pt-hero-sub { font-size: .8rem; opacity: .75; margin-top: .2rem; }
.pt-up { color: var(--pt-up); } .pt-down { color: var(--pt-down); }
.pt-live { color: var(--pt-up); }
.pt-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: .6rem; margin-top: 1rem; }
/* exactly four boxes (Home with a total return beside the price change):
   one row of four, two rows of two on a phone - never three and a lone one */
.pt-stats:has(> .pt-stat:nth-child(4):last-child) {
  grid-template-columns: repeat(4, minmax(0, 1fr)); }
.pt-stat { border: 1px solid var(--pt-line); border-radius: .5rem;
  padding: .55rem .7rem; min-width: 0; }
.pt-stat-label { font-size: .75rem; opacity: .7; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.pt-stat-value { font-size: 1.1rem; font-weight: 600; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
.pt-stat-sub { font-size: .8rem; font-weight: 600; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
/* phones: three boxes in a row leave about 90px each, so a label or note
   wraps onto a second line instead of running into the next box. A value's
   size follows its box (cqi, a share of the box's width; .75rem to .9rem),
   which fits $1,234,567.00 in a 113px box, and a longer amount or a narrower
   box wraps after a thousands comma (_stat_row marks those spots) rather
   than being cut off */
@media (max-width: 640px) { .pt-hero-value { font-size: 2.2rem; }
  .pt-stat { padding: .5rem .5rem; container-type: inline-size; }
  .pt-stat-value { font-size: .9rem; font-size: clamp(.75rem, 14cqi, .9rem);
    white-space: normal; line-height: 1.3; }
  .pt-stat-label, .pt-stat-sub { white-space: normal; overflow-wrap: break-word; }
  .pt-stats:has(> .pt-stat:nth-child(4):last-child) {
    grid-template-columns: repeat(2, minmax(0, 1fr)); } }
/* summary tiles that open a window (Learn the basics; Income, Activity,
   Watchlist and Ask Northwend in the calm view): lift a little on hover */
[class*="st-key-pt_tile_"] { transition: border-color .2s ease, transform .2s ease; }
[class*="st-key-pt_tile_"]:hover { border-color: var(--pt-compass) !important;
  transform: translateY(-2px); }
.pt-alloc-title { font-size: .9rem; font-weight: 600; margin-bottom: .35rem; }
.pt-alloc-bar { display: flex; gap: 2px; height: 14px; border-radius: 4px;
  overflow: hidden; margin-bottom: .6rem; }
.pt-alloc-seg { height: 100%; min-width: 3px; }
/* the legend's one column never grows past its card (a grid track sizes to
   its widest line otherwise); a long label wraps, and the percentage and
   amount keep their place at the right, top-aligned with the label */
.pt-legend { display: grid; grid-template-columns: minmax(0, 1fr); gap: .3rem;
  margin-bottom: .75rem; }
.pt-legend-row { display: flex; align-items: flex-start; gap: .5rem; font-size: .9rem;
  line-height: 1.4; min-width: 0; }
.pt-swatch { width: 10px; height: 10px; border-radius: 3px; flex: none; margin-top: .35em; }
.pt-legend-label { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
.pt-legend-pct { flex: none; font-weight: 600; font-variant-numeric: tabular-nums; }
.pt-legend-val { flex: none; opacity: .75; font-variant-numeric: tabular-nums; min-width: 5.5rem;
  text-align: right; }
.pt-acct { margin-bottom: .8rem; }
.pt-acct .pt-legend-row { margin-bottom: .3rem; }
.pt-alloc-bar.pt-mini { height: 8px; margin-bottom: 0; }
.pt-warn { color: var(--pt-warn); } .pt-muted { opacity: .7; }
/* a goal with nothing invested yet: a calm compass chip, not a red "Behind" */
.pt-chip.pt-start { color: var(--pt-link); background: var(--pt-compass-soft);
  border-color: transparent; }
.pt-wl-name { font-size: .85rem; opacity: .7; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.pt-wl-quote { text-align: right; font-size: .9rem; line-height: 1.3;
  font-variant-numeric: tabular-nums; white-space: nowrap; }
/* a watchlist row stays on one line; a long name shortens with "..." instead */
[class*="st-key-wlrow_"] { flex-wrap: nowrap !important; }
[class*="st-key-wlrow_"] > div { min-width: 0; }
.pt-chip { display: inline-block; padding: .1rem .6rem; border-radius: 999px; font-size: .8rem;
  font-weight: 600; border: 1px solid currentColor; }
.pt-goal-top { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
.pt-goal-pct { font-weight: 600; }
.pt-goal-track { height: 10px; border-radius: 5px; background: var(--pt-sunken);
  overflow: hidden; margin: .5rem 0 .4rem; }
.pt-goal-fill { height: 100%; border-radius: 5px; background: var(--pt-compass); }
.pt-goal-sub { font-size: .8rem; opacity: .7; }
.pt-mix-track { position: relative; height: 8px; border-radius: 4px;
  background: var(--pt-sunken); margin: 0 0 .6rem; }
.pt-mix-fill { height: 100%; border-radius: 4px; background: var(--pt-compass); }
.pt-mix-target { position: absolute; top: -3px; width: 3px; height: 14px; border-radius: 1px;
  margin-left: -1px; background: currentColor; }
</style>""")


@functools.lru_cache(maxsize=1)
def _topo_css() -> str:
    """The expedition's contour lines behind every page (ROADMAP T1), one
    drawing per theme, built into the stylesheet: Streamlit's static files
    are served as plain text, which browsers won't draw as a picture."""
    from urllib.parse import quote
    rules = []
    for theme, sel in (("light", ':root:not([data-pt-theme="dark"])'),
                       ("dark", ':root[data-pt-theme="dark"]')):
        with open(os.path.join(HERE, "static", f"topo-{theme}.svg"), encoding="utf-8") as fh:
            svg = quote(fh.read().strip(), safe=" =:/,.-#")
        rules.append(f'{sel} [data-testid="stMain"] {{ background-image: '
                     f'url("data:image/svg+xml,{svg}"); }}')
    return "<style>" + "\n".join(rules) + "</style>"


st.html(_topo_css())

if STAGING:
    st.html("<div class='pt-staging' role='note'>Staging copy, for trying changes before "
            "they go live. Use test data only: this is not the real Northwend.</div>")

# The old address once the app has moved (hosting.MOVED_TO): only say where it is now.
if hosting.moved_to():
    _new = hosting.moved_link(hosting.moved_to(), st.query_params.to_dict())
    _, _mid, _ = st.columns([1, 1.4, 1])
    with _mid:
        st.title(f"{APP_ICON} {APP_NAME} has moved")
        st.markdown(f"{APP_NAME} now lives at its own address. Your account, holdings and "
                    "plan came along - sign in there as usual.")
        st.link_button(f"Go to {APP_NAME}", _new, type="primary", width="stretch")
        st.caption(f"Worth updating your bookmark: {hosting.moved_to()}")
    st.stop()


def _visitor_ip():
    """The visitor's address for sign-up and email limits - from the proxy's
    header on our own host (hosting.CLIENT_IP_HEADER)."""
    return hosting.client_ip(st.context.headers, st.context.ip_address)

SESSION_COOKIE = "pt_session"


def _cookie_script(token: str | None) -> str:
    """JS that stores the stay-signed-in token in a browser cookie, or with
    None deletes it. Streamlit can read cookies (st.context.cookies) but not
    set them, so the page does it. Secure on https; Lax keeps it off
    cross-site requests."""
    if token:
        value = f"{SESSION_COOKIE}={token}; Max-Age={auth.SESSION_DAYS * 86400}"
    else:
        value = f"{SESSION_COOKIE}=; Max-Age=0"
    return ("<script>document.cookie = " + json.dumps(value + "; Path=/; SameSite=Lax")
            + " + (location.protocol === 'https:' ? '; Secure' : '');</script>")


def _session_cookie() -> str | None:
    """The browser's stay-signed-in token, or None. Only ever a string -
    anything else (an empty or odd cookie jar) counts as no cookie."""
    value = st.context.cookies.get(SESSION_COOKIE)
    return value if isinstance(value, str) and value else None


def _app_address() -> str:
    """This app's web address without its ?query, for links to send people."""
    base = (st.context.url or "").split("?")[0].split("#")[0]
    if base:
        return base
    host = st.context.headers.get("host") or ""   # e.g. behind a proxy, or older Streamlit
    if not host:
        return ""
    local = host.startswith(("localhost", "127.0.0.1"))
    proto = st.context.headers.get("x-forwarded-proto") or ("http" if local else "https")
    return f"{proto}://{host}/"


def _send_confirmation(user_id) -> tuple[bool, str]:
    """Email this account a confirm-your-email link (auth.start_confirmation,
    mailer.py). (sent, a message to show)."""
    conn = connect(DB)
    try:
        res = auth.start_confirmation(conn, user_id, ip=_visitor_ip())
    finally:
        conn.close()
    if not res["ok"]:
        return False, res["error"]
    if not mailer.confirm_email(res["to"], f"{_app_address()}?confirm={res['token']}",
                                auth.CONFIRM_DAYS):
        return False, "We couldn't send the email just now. Please try again in a few minutes."
    return True, (f"We sent a link to {res['to']}. It can take a minute - check your spam "
                  "folder too.")


def _render_disclosures(*, summary=True):
    """The About and disclosures text (disclosures.py) - the About page, and
    on the login screen for people who haven't signed in."""
    if summary:
        st.markdown(disclosures.SUMMARY)
    for title, body in disclosures.SECTIONS:
        st.subheader(title, anchor=False)
        st.markdown(body.strip())
    st.caption(f"Last updated {disclosures.LAST_UPDATED}.")


def _toggle_about():
    st.session_state["show_about"] = not st.session_state.get("show_about")


def _invite_setup(token: str) -> bool:
    """The page a client's setup link opens: choose a password for the
    account their advisor made, then they're signed in. False until then."""
    conn = connect(DB)
    try:
        info = auth.invite_info(conn, token)
    finally:
        conn.close()
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title(f"{APP_ICON} {APP_NAME}")
        if info is None:
            st.error("This setup link has expired or was already used. Ask your advisor "
                     "for a new one.")
            if st.button("Go to sign in", type="primary"):
                del st.query_params["invite"]
                st.rerun()
            return False
        st.subheader("Set up your login", anchor=False)
        st.caption(f"Your advisor set up a {APP_NAME} account for you. Choose a password "
                   "only you know - your advisor never sees it.")
        with st.form("invite_form", border=True):
            st.text_input("Username", value=info["username"], disabled=True,
                          help="You'll sign in with this.")
            pw = st.text_input("Choose a password", type="password", key="invite_pw",
                               help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.")
            again = st.text_input("Type it again", type="password", key="invite_pw_again")
            remember = st.checkbox(f"Stay signed in on this device ({auth.SESSION_DAYS} days)",
                                   value=True, key="invite_remember",
                                   help="Leave this off on a shared or public computer.")
            submitted = st.form_submit_button("Create my login", type="primary",
                                              width="stretch")
        st.caption(disclosures.SUMMARY)
    if not submitted:
        return False
    if pw != again:
        mid.error("The two passwords don't match.")
        return False
    conn = connect(DB)
    try:
        result = auth.accept_invite(conn, token, pw)
        session = (auth.create_session(conn, result["user_id"])
                   if result["ok"] and remember else None)
    finally:
        conn.close()
    if not result["ok"]:
        mid.error(result["error"])
        return False
    st.session_state.clear()  # whoever was signed in on this browser before
    st.session_state["user_id"] = result["user_id"]
    st.session_state["username"] = result["username"]
    st.session_state["session_token"] = session
    # straight to the goals and risk questions, so the advisor has a ready
    # profile before the first meeting (ROADMAP G6)
    st.session_state["page"] = "Get started"
    st.session_state["import_flash"] = (
        "Your login is ready. Welcome! First, a few quick questions about your goals and how "
        "you feel about ups and downs - about two minutes. Your advisor sees your answers.")
    del st.query_params["invite"]
    st.rerun()


def _show_signup(flag):
    """Switch the sign-in screen between Log in and Create account."""
    st.session_state["show_signup"] = flag
    st.session_state.pop("signup_opened", None)
    if not flag and "signup" in st.query_params:
        del st.query_params["signup"]


SIGNUP_ROLES = {"investor": "For my own investing", "advisor": "I'm a financial advisor"}


def _signup() -> bool:
    """The Create account page (auth.sign_up): how they'll use Northwend, an
    email, a password, and agreeing to the disclosures. A new account is
    signed in straight away and starts on Get started. An advisor's account
    starts as an investor account with a request for advisor access
    (auth.request_advisor) that the admin approves. Linkable as ?signup=1, or
    ?signup=advisor to start on the advisor choice. False until it's made."""
    # when the form first appeared - one sent sooner than a person could is asked again
    st.session_state.setdefault("signup_opened", time.time())
    st.session_state.setdefault("signup_role", "advisor" if st.query_params.get("signup")
                                == "advisor" else "investor")
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title(f"{APP_ICON} {APP_NAME}")
        st.subheader("Create your account", anchor=False)
        st.caption(f"Free while {APP_NAME} is in beta. Your email is just your login: it's never "
                   "shown to anyone or sent to the AI, and you never connect a brokerage. We don't sell "
                   "investments or take commissions. You can "
                   "start with an example portfolio or percentages instead of real numbers.")
        role = st.segmented_control("How will you use Northwend?", list(SIGNUP_ROLES),
                                    format_func=SIGNUP_ROLES.get, key="signup_role",
                                    width="stretch") or "investor"
        firm = licence = ""
        with st.form("signup_form", border=True):
            email = st.text_input("Email", key="signup_email", autocomplete="email",
                                  placeholder="name@example.com")
            pw = st.text_input("Choose a password", type="password", key="signup_pw",
                               autocomplete="new-password",
                               help=f"At least {auth.MIN_PASSWORD_LENGTH} characters, and one "
                                    "you don't use anywhere else.")
            again = st.text_input("Type it again", type="password", key="signup_pw_again",
                                  autocomplete="new-password")
            st.text_input("Website", key="signup_website")  # hidden (see the CSS); bots fill it
            if role == "advisor":
                st.caption("Advisor tools are for licensed professionals, so we check each "
                           "request first - usually within two working days. Until then you "
                           "can explore Northwend as an investor.")
                firm = st.text_input("Firm name", key="signup_firm", max_chars=100)
                licence = st.text_input("CRD or licence number", key="signup_licence",
                                        max_chars=40,
                                        help="Your individual CRD number (FINRA BrokerCheck) or "
                                             "the licence number where you're registered.")
            adult = st.checkbox(f"I'm {disclosures.MIN_AGE} or older", key="signup_adult")
            agreed = st.checkbox("I've read and agree to the About and disclosures",
                                 key="signup_agree",
                                 help="What the app is, what's stored and what's sent to the "
                                      "AI - open it below.")
            remember = st.checkbox(f"Stay signed in on this device ({auth.SESSION_DAYS} days)",
                                   value=True, key="signup_remember",
                                   help="Leave this off on a shared or public computer.")
            submitted = st.form_submit_button("Create account", type="primary", width="stretch")
        st.caption("We'll email you a link to confirm your address - it unlocks the AI guide "
                   "and lets you reset your password if you ever forget it.")
        with st.container(horizontal=True):
            st.button("Hide about and disclosures" if st.session_state.get("show_about")
                      else "About and disclosures", key="signup_about", type="tertiary",
                      on_click=_toggle_about)
            st.button("Already have an account? Sign in", key="signup_to_login",
                      type="tertiary", on_click=_show_signup, args=(False,))
    if st.session_state.get("show_about"):
        with mid.container(border=True):
            _render_disclosures()
    if not submitted:
        return False
    if pw != again:
        mid.error("The two passwords don't match.")
        return False
    if role == "advisor" and auth.advisor_request_error(firm, licence):
        mid.error(auth.advisor_request_error(firm, licence))
        return False
    conn = connect(DB)
    try:
        result = auth.sign_up(conn, email, pw, agreed=agreed, adult=adult,
                              terms_version=disclosures.LAST_UPDATED,
                              ip=_visitor_ip(),
                              seconds_open=time.time() - st.session_state["signup_opened"],
                              honeypot=st.session_state.get("signup_website") or "")
        if result["ok"]:
            # they just agreed to this version, so no "worth a quick read" banner
            prefs.save(conn, result["user_id"], {"disclosures_seen": disclosures.LAST_UPDATED})
            session = auth.create_session(conn, result["user_id"]) if remember else None
            if role == "advisor":
                auth.request_advisor(conn, result["user_id"], firm, licence)
    finally:
        conn.close()
    if not result["ok"]:
        mid.error(result["error"])
        return False
    if role == "advisor":  # a failure is logged by mailer; `advisor-requests` lists it anyway
        mailer.advisor_request(result["username"], firm.strip(), licence.strip(),
                               _app_address())
    sent, note = _send_confirmation(result["user_id"])
    st.session_state.clear()  # whoever was signed in on this browser before
    st.session_state["user_id"] = result["user_id"]
    st.session_state["username"] = result["username"]
    st.session_state["session_token"] = session
    if role == "advisor":
        st.session_state["import_flash"] = (
            f"Your account is ready. Welcome to {APP_NAME}! We're checking your advisor "
            "details; advisor tools appear once they're approved. Until then, have a look "
            "around as an investor.")
    if sent:
        # just signed up: one short line about the link, so the welcome screen
        # stays in view (a phone has room for little else); the fuller card with
        # "Send it again" comes back in later sessions
        st.session_state["email_brief"] = True
    else:
        st.session_state["email_flash"] = (False, note)
    if "signup" in st.query_params:
        del st.query_params["signup"]
    st.rerun()


def _show_forgot(flag):
    """Switch the sign-in screen between Log in and Forgot password."""
    st.session_state["show_forgot"] = flag
    st.session_state.pop("forgot_sent", None)


def _forgot() -> bool:
    """The "Forgot password?" page: an email address in, a reset link out
    (auth.request_password_reset, mailer.py). The answer is the same whether
    or not there's an account, so it can't be used to find out who has one."""
    submitted = False
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title(f"{APP_ICON} {APP_NAME}")
        st.subheader("Reset your password", anchor=False)
        sent_to = st.session_state.get("forgot_sent")
        if sent_to:
            st.success(f"If there's an account for {sent_to}, we've sent it a link to choose a "
                       f"new password. The link works for {auth.RESET_MINUTES} minutes - check "
                       "your spam folder too.")
        else:
            st.caption("Enter the email you signed up with and we'll send you a link to choose "
                       "a new password.")
            with st.form("forgot_form", border=True):
                email = st.text_input("Email", key="forgot_email", autocomplete="email",
                                      placeholder="name@example.com")
                submitted = st.form_submit_button("Send me a link", type="primary",
                                                  width="stretch")
        st.caption("Your account was set up by an advisor or an administrator? Ask them to "
                   "reset your password.")
        st.button("Back to sign in", key="forgot_back", type="tertiary",
                  on_click=_show_forgot, args=(False,))
    if sent_to or not submitted:
        return False
    conn = connect(DB)
    try:
        res = auth.request_password_reset(conn, email, ip=_visitor_ip())
    finally:
        conn.close()
    if not res["ok"]:
        mid.error(res["error"])
        return False
    if res["token"]:  # a failure is logged by mailer; the answer must look the same either way
        mailer.reset_password(res["to"], f"{_app_address()}?reset={res['token']}",
                              auth.RESET_MINUTES)
    st.session_state["forgot_sent"] = auth.normalize_email(email)
    st.rerun()


def _reset_setup(token: str) -> bool:
    """The page a reset link opens: choose a new password, then signed in
    (everywhere else signed out). False until then."""
    conn = connect(DB)
    try:
        info = auth.reset_info(conn, token)
    finally:
        conn.close()
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title(f"{APP_ICON} {APP_NAME}")
        if info is None:
            st.error("This reset link has expired or was already used. You can ask for a new one.")
            if st.button("Go to sign in", type="primary"):
                del st.query_params["reset"]
                st.rerun()
            return False
        st.subheader("Choose a new password", anchor=False)
        with st.form("reset_form", border=True):
            st.text_input("Email", value=info["email"], disabled=True)
            pw = st.text_input("New password", type="password", key="reset_pw",
                               autocomplete="new-password",
                               help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.")
            again = st.text_input("Type it again", type="password", key="reset_pw_again",
                                  autocomplete="new-password")
            remember = st.checkbox(f"Stay signed in on this device ({auth.SESSION_DAYS} days)",
                                   value=True, key="reset_remember",
                                   help="Leave this off on a shared or public computer.")
            submitted = st.form_submit_button("Save new password", type="primary",
                                              width="stretch")
        st.caption("Saving signs you out on every other device.")
    if not submitted:
        return False
    if pw != again:
        mid.error("The two passwords don't match.")
        return False
    conn = connect(DB)
    try:
        result = auth.reset_password(conn, token, pw)
        session = (auth.create_session(conn, result["user_id"])
                   if result["ok"] and remember else None)
    finally:
        conn.close()
    if not result["ok"]:
        mid.error(result["error"])
        return False
    st.session_state.clear()  # whoever was signed in on this browser before
    st.session_state["user_id"] = result["user_id"]
    st.session_state["username"] = result["username"]
    st.session_state["session_token"] = session
    st.session_state["import_flash"] = "Your new password is saved. Welcome back!"
    del st.query_params["reset"]
    st.rerun()


def _take_confirm_link():
    """A confirm-your-email link (?confirm=...): mark the email confirmed and
    say so - on the sign-in screen, and once signed in."""
    token = st.query_params.get("confirm")
    if not token:
        return
    conn = connect(DB)
    try:
        res = auth.confirm_email(conn, str(token))
    finally:
        conn.close()
    del st.query_params["confirm"]
    if res["ok"]:
        msg = f"Your email is confirmed - thank you! The AI guide, Ask {GUIDE}, is ready."
    else:
        msg = res["error"] + " If you still need to confirm, send a new link after signing in."
    st.session_state["email_flash"] = (res["ok"], msg)
    st.session_state["email_state"] = None  # look it up again
    st.session_state["login_notice"] = msg + ("" if st.session_state.get("user_id")
                                              else " Sign in to continue.")


def _take_email_change_link():
    """A change-your-email link (?email_change=..., the Account page): the new
    address becomes the account's, the old one is told, and a signed-in
    browser follows a login that was the old email."""
    token = st.query_params.get("email_change")
    if not token:
        return
    conn = connect(DB)
    try:
        res = auth.confirm_email_change(conn, str(token))
    finally:
        conn.close()
    del st.query_params["email_change"]
    if res["ok"]:
        if res["old_email"]:
            mailer.email_changed(res["old_email"], res["email"], _app_address())
        if st.session_state.get("user_id") == res["user_id"]:
            st.session_state["username"] = res["username"]
        msg = f"Your email is now {res['email']}."
        if res["username"] == res["email"]:
            msg += " Sign in with it from now on."
    else:
        msg = res["error"]
    st.session_state["email_flash"] = (res["ok"], msg)
    st.session_state["email_state"] = None  # look it up again
    st.session_state["login_notice"] = msg + ("" if st.session_state.get("user_id")
                                              else " Sign in to continue.")


def _login() -> bool:
    """Per-account login. Accounts are made by an admin (manage_users.py),
    an advisor for a client, or by people themselves on the Create account
    page (_signup). Sets
    st.session_state["user_id"]/["username"] on success. Generic error
    message on any failure (unknown username OR wrong password) so the
    login screen never reveals which username exists.

    A new browser session (a reload, a phone reopening the tab) first tries
    the stay-signed-in cookie; the token is checked against the database
    every time, so logging out or changing the password ends it."""
    invite = st.query_params.get("invite")
    if invite:  # a client's setup link (auth.create_invite)
        return _invite_setup(str(invite))
    reset = st.query_params.get("reset")
    if reset:  # a reset-your-password link (auth.request_password_reset)
        return _reset_setup(str(reset))
    _take_confirm_link()
    _take_email_change_link()
    # signed in, however it happened: two-step sign-in comes next (views/two_step.py)
    if st.session_state.get("user_id"):
        return _two_step_gate()
    cookie = _session_cookie()
    if cookie and not st.session_state.get("signed_out"):
        conn = connect(DB)
        try:
            found = auth.session_user(conn, cookie)
        finally:
            conn.close()
        if found:
            st.session_state["user_id"], st.session_state["username"] = found
            st.session_state["session_token"] = cookie
            return _two_step_gate()
        st.session_state["signed_out"] = True  # a dead cookie: remove it below
    if st.session_state.get("signed_out") and cookie:
        st.html(_cookie_script(None), unsafe_allow_javascript=True)
    if st.session_state.get("show_signup", "signup" in st.query_params):
        return _signup()
    if st.session_state.get("show_forgot"):
        return _forgot()

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title(f"{APP_ICON} {APP_NAME}")
        st.caption(f"{TAGLINE} Sign in to see your portfolio.")
        _notice = st.session_state.get("login_notice")
        if _notice:
            st.info(_notice)
        with st.form("login_form", border=True):
            user = st.text_input("Email or username", key="login_user", autocomplete="username")
            pw = st.text_input("Password", type="password", key="login_pw",
                               autocomplete="current-password")
            remember = st.checkbox(f"Stay signed in on this device ({auth.SESSION_DAYS} days)",
                                   value=True, key="login_remember",
                                   help="Leave this off on a shared or public computer.")
            submitted = st.form_submit_button("Log in", type="primary", width="stretch")
        st.button("Forgot password?", key="login_forgot", type="tertiary",
                  on_click=_show_forgot, args=(True,))
        st.button("New here? Create an account", key="login_to_signup", width="stretch",
                  on_click=_show_signup, args=(True,))
        st.caption(disclosures.SUMMARY)
        st.button("Hide about and disclosures" if st.session_state.get("show_about")
                  else "About and disclosures", key="login_about", type="tertiary",
                  on_click=_toggle_about)
    if st.session_state.get("show_about"):
        with mid.container(border=True):
            _render_disclosures(summary=False)  # the summary is just above
    if submitted:
        if not user or not pw:
            mid.error("Enter your email or username, and your password.")
            return False
        conn = connect(DB)
        try:
            # behind the lockout: too many wrong passwords locks the username
            result = auth.attempt_login(conn, user, pw)
            user_id = result["user_id"]
            token = auth.create_session(conn, user_id) if user_id is not None and remember else None
            # as stored, not as typed (an email works in any letter case)
            username = auth.get_username(conn, user_id) if user_id is not None else None
        finally:
            conn.close()
        if user_id is not None:
            st.session_state.pop("signed_out", None)
            st.session_state.pop("login_notice", None)
            st.session_state["user_id"] = user_id
            st.session_state["username"] = username
            st.session_state["session_token"] = token
            st.rerun()
        if result["locked_minutes"]:
            m = result["locked_minutes"]
            mid.error(f"Too many attempts. Try again in {m} minute{'s' if m != 1 else ''}, "
                      "or choose a new one with Forgot password? (if an advisor manages your "
                      "account, ask them).")
        elif result["attempts_left"] <= 2:
            mid.error(f"Wrong email, username or password. {result['attempts_left']} more "
                      f"attempt{'s' if result['attempts_left'] != 1 else ''} before a "
                      f"{auth.LOCKOUT_MINUTES}-minute lock.")
        else:
            mid.error("Wrong email, username or password.")
    return False


def _logout():
    # Full session_state reset, not just clearing user_id/username - every
    # other key (hide_amounts, col_keys, last_open_snapshot, pill
    # selections, etc.) was populated for the PREVIOUS account and would
    # otherwise leak into the next login on the same browser tab even
    # though each account's own on-disk prefs file is already correctly
    # separated (PREFS_PATH is per-user) - the in-memory session state
    # isn't, unless explicitly cleared here. No st.rerun() needed - an
    # on_click callback is always followed by an automatic rerun, and
    # calling it explicitly here just logs a "no-op" warning.
    # The stay-signed-in session ends in the database (so the cookie is dead
    # even if deleting it fails), and "signed_out" stops the login page from
    # using the cookie and has it deleted from the browser.
    conn = connect(DB)
    try:
        auth.end_session(conn, st.session_state.get("session_token")
                         or _session_cookie())
    finally:
        conn.close()
    st.session_state.clear()
    st.session_state["signed_out"] = True


# two-step sign-in: the code / setup pages _login() shows after the password
_view("two_step")

if not _login():
    st.stop()
# Just signed in with "stay signed in": put the token in the browser's cookie.
# Rendered on every run until a reload shows the browser has it, so a rerun
# right after login can't drop it.
if (st.session_state.get("session_token")
        and _session_cookie() != st.session_state["session_token"]):
    st.html(_cookie_script(st.session_state["session_token"]), unsafe_allow_javascript=True)

# Advisor mode: user_id is who logged in; active_user_id is whose data is
# showing. Every query below goes through USER_ID, so it's resolved here,
# re-checked against the database on every run - never trusted from
# session state alone.
LOGIN_ID = st.session_state["user_id"]
_conn = connect(DB)
try:
    # the login's own row (password stamp, advisor, admin, name): as the
    # two-step gate read it just now, in this run (views/two_step.py)
    _gate = _gate_read(LOGIN_ID)
    _me = (auth.login_facts_of(_gate[1]) if _gate is not None
           else auth.login_facts(_conn, LOGIN_ID))
    # A password change (here, on another device, or by an admin or advisor)
    # signs out tabs that are already open, not just the saved cookies.
    _stamp = _me["stamp"]
    if st.session_state.setdefault("pw_stamp", _stamp) != _stamp:
        st.session_state.clear()
        st.session_state["signed_out"] = True
        st.session_state["login_notice"] = "Your password was changed. Sign in again."
        st.rerun()
    IS_ADVISOR = _me["is_advisor"]
    # the Admin portal (admin.py; granted only from the command line)
    IS_ADMIN = _me["is_admin"]
    # what they asked to be called (the Account page), else their login
    MY_NAME = _me["display_name"] or st.session_state["username"]
    CLIENTS = auth.list_clients(_conn, LOGIN_ID) if IS_ADVISOR else []
    # an investor account that asked for advisor access (shown in the name menu)
    ADVISOR_REQUEST = None if IS_ADVISOR else auth.advisor_request(_conn, LOGIN_ID)
    if "active_user_id" not in st.session_state:
        # a fresh session (reload, bookmark): start on the client in the address,
        # if any - re-checked by can_view just below, like every other run
        try:
            st.session_state["active_user_id"] = int(st.query_params.get("client", LOGIN_ID))
        except ValueError:
            pass
    _active = st.session_state.get("active_user_id", LOGIN_ID)
    if not auth.can_view(_conn, LOGIN_ID, _active):
        _active = LOGIN_ID
    ACCOUNT_LABELS = accounts.labels(_conn, _active)
    # the latest holdings' date (load() reads that snapshot below)
    _LATEST_SNAPSHOT = latest_snapshot(_conn, _active)
    HAS_HOLDINGS = _LATEST_SNAPSHOT is not None
    # where they came from (load() below reuses it): the example portfolio
    # isn't an account they've opened, so Learn keeps its place while it's all
    # there is (HAS_REAL_HOLDINGS)
    _LATEST_SOURCE = snapshot_source(_conn, _active, _LATEST_SNAPSHOT) if HAS_HOLDINGS else None
    HAS_REAL_HOLDINGS = HAS_HOLDINGS and _LATEST_SOURCE != SAMPLE_SOURCE
    # A client whose account an advisor manages: the plan, target mix, alert
    # limits and imports are the advisor's, so the client's view is read-only
    # for those. MY_ADVISOR_CARD is how the advisor presents themselves.
    MY_ADVISOR = None if IS_ADVISOR else advising.advisor_of(_conn, LOGIN_ID)
    MY_ADVISOR_CARD = ({**(prefs.load(_conn, MY_ADVISOR).get("advisor_card") or {}),
                        "username": auth.get_username(_conn, MY_ADVISOR)} if MY_ADVISOR else {})
    # ...except imports, which the advisor can open up per client (Clients page)
    CLIENT_CAN_IMPORT = bool(MY_ADVISOR) and advising.client_can_import(_conn, LOGIN_ID)
finally:
    _conn.close()
USER_ID = _active
IS_MANAGED_CLIENT = MY_ADVISOR is not None
CAN_MANAGE = not IS_MANAGED_CLIENT         # may edit this account's plan, limits, imports
CAN_IMPORT = CAN_MANAGE or CLIENT_CAN_IMPORT  # may import statements into this account
ON_CLIENT = IS_ADVISOR and USER_ID != LOGIN_ID   # an advisor working on a client's account
# Client mode: an advisor's client - and their advisor in their account, who
# sees what they see. Their plan and recommendations are the advisor's, so
# the beginner's example funds, example mix and practice money stay out of
# it, Learn is never required, and Home's next step speaks for the advisor
# (route.advisor_step). Milestones and gear stay: learning and habits only.
CLIENT_MODE = IS_MANAGED_CLIENT or ON_CLIENT
# The investor experience: investors, clients, and an advisor looking at a
# client's account (they see what the client sees). An advisor's own
# portfolio is the advisor experience.
INVESTOR_VIEW = not IS_ADVISOR or ON_CLIENT
# The portfolio page is "Home" in the investor app and "Portfolio" in the
# advisor app, whose home is Your clients. (Its internal name stays
# "Dashboard"; old ?page=dashboard links still open it.)
PAGE_LABELS["Dashboard"] = "Portfolio" if IS_ADVISOR else "Home"
# Get started is "Learn" in the investor menu (ROADMAP S4); an advisor's
# copy, in a client's account too, keeps "Get started".
if not IS_ADVISOR:
    PAGE_LABELS["Get started"] = "Learn"
# a managed client's page from their advisor: notes, reports and proposals
if IS_MANAGED_CLIENT:
    PAGE_LABELS["Advisor notes"] = "Your advisor"
st.session_state["active_user_id"] = USER_ID
ACTIVE_NAME = (MY_NAME if USER_ID == LOGIN_ID
               else dict(CLIENTS).get(USER_ID, "client"))
# where this account's settings lived before they moved into the database;
# read once, the first time, so they carry over (prefs.py)
PREFS_PATH = os.path.join(HERE, f".dashboard_prefs.{USER_ID}.json")

# Two experiences (ROADMAP G1). Investors - and clients of an advisor - get the
# investor app: Get started leads for an account with nothing imported yet
# (and is where it lands); once there are holdings it moves to the end as a
# reference. Advisors get the advisor app: it opens on Your clients, and the
# portfolio pages below it are for whichever account is being viewed; Get
# started only appears when that's a client's.
if IS_ADVISOR:
    _start = ["Get started"] if ON_CLIENT else []
    PAGES = ["Clients", *([] if HAS_HOLDINGS else _start),
             "Dashboard", "Plan", *(["Advisor notes"] if ON_CLIENT else []),
             "Watchlist", "Activity", "Income", "AI Assistant",
             *(_start if HAS_HOLDINGS else []), "Account", "About"]
else:
    # an advisor's client lands on Home (their advisor's next step), never
    # on Learn: it's there for them, not required
    _learn_last = HAS_REAL_HOLDINGS or IS_MANAGED_CLIENT
    PAGES = [*([] if _learn_last else ["Get started"]),
             "Dashboard", "Plan", *(["Advisor notes"] if IS_MANAGED_CLIENT else []),
             "Watchlist", "Activity", "Income", "AI Assistant",
             *(["Get started"] if _learn_last else []), "Account", "About"]
if IS_ADMIN:
    PAGES.append("Admin")

# The menu: a bar along the top (the phone tab bar below shows the same tabs),
# everything on it at once - nothing hidden behind a "More". Investors get
# Home, Plan, Learn, Ask Northwend and Money (+ Your advisor for a managed
# client); advisors Clients, Portfolio, Plan, Notes (in a client's account),
# Money and Ask. Money is one page with a tab each for Income, Activity and
# Watchlist (MONEY_PAGES): those stay pages of their own inside (their
# ?page= names, _go("Income") and the views' `if PAGE == ...`), the menu
# just groups them. Account, About, Admin and Log out are in the name menu
# at the right (ACCOUNT_MENU). PAGES stays every page this account can open
# (the address, ?page=, checks against it). Always in this order, before
# holdings and after (where they land is PAGES[0]): a tab never moves under
# someone's thumb.
MONEY = "Money"
MONEY_PAGES = ("Income", "Activity", "Watchlist")
if IS_ADVISOR:
    NAV = ["Clients", "Dashboard", "Plan", *(["Advisor notes"] if ON_CLIENT else []),
           MONEY, "AI Assistant"]
else:
    NAV = ["Dashboard", "Plan", "Get started", "AI Assistant", MONEY,
           *(["Advisor notes"] if IS_MANAGED_CLIENT else [])]
ACCOUNT_MENU = [p for p in ("Account", "About", "Admin") if p in PAGES]
# the top bar's words where they're shorter than the page's own name (an
# advisor's bar holds more); the button's tooltip gives the full name
NAV_SHORT = ({"Clients": "Clients", "Advisor notes": "Notes", "AI Assistant": "Ask"}
             if IS_ADVISOR else {})


def _nav_label(item):
    return NAV_SHORT.get(item, item if item == MONEY else _label(item))


def _slug(page):
    """A page's name in the address: 'Ask Northwend' -> 'ask-northwend'."""
    return _label(page).lower().replace(" ", "-")


# addresses saved before a page was renamed still open it - and a link made in
# the other experience (Home / Portfolio, Learn / Get started)
OLD_SLUGS = {"ask-sage": "AI Assistant", "clients": "Clients", "dashboard": "Dashboard",
             "home": "Dashboard", "portfolio": "Dashboard",
             "get-started": "Get started", "learn": "Get started",
             # Money opens on its first tab; each tab keeps its own name
             # (?page=income, activity, watchlist)
             "money": "Income",
             # a managed client's "Your advisor" is the advisor's "Advisor notes"
             # (the report emails link to ?page=advisor-notes)
             "advisor-notes": "Advisor notes", "your-advisor": "Advisor notes"}

if "page" not in st.session_state:
    # a fresh session: start on the page in the address (?page=plan), if it's
    # one this account can open
    _wanted = str(st.query_params.get("page", "")).lower()
    st.session_state["page"] = {_slug(p): p for p in PAGES}.get(
        _wanted, OLD_SLUGS.get(_wanted) if OLD_SLUGS.get(_wanted) in PAGES else PAGES[0])
if st.session_state.get("page") not in PAGES:
    st.session_state["page"] = PAGES[0]

# kept when an advisor switches accounts; everything else is per-account
_KEEP_ON_SWITCH = ("user_id", "username", "page", "session_token", "pw_stamp", "two_step_ok")


def _go(page):
    st.session_state["page"] = page


def _advisor_display_name():
    """The managing advisor's name as they've chosen to show it."""
    card = MY_ADVISOR_CARD
    return (card.get("name") or card.get("username") or "your advisor") +         (f", {card['firm']}" if card.get("firm") else "")


def _md_name(name):
    """A person's name or email inside st.markdown, shown as typed: markdown
    characters escaped, and an email isn't turned into a mailto link (an
    invisible word joiner before the @ stops the autolink)."""
    text = re.sub(r"([\\`*_{}\[\]<>()#+!|~$])", r"\\\1", str(name or ""))
    return text.replace("@", "\u2060@")


def _advisor_names(card, username):
    """An advisor's (name for an email's text, name for its From line) from
    their card (How clients see you): "Dana Ruiz (Ruiz Wealth)" and
    "Dana Ruiz, Ruiz Wealth" - the login when there's no name yet."""
    name = card.get("name") or username
    firm = card.get("firm")
    return (f"{name} ({firm})" if firm else name), (f"{name}, {firm}" if firm else name)


def _switch_to(account_id):
    for k in list(st.session_state.keys()):
        if k not in _KEEP_ON_SWITCH:
            del st.session_state[k]
    st.session_state["active_user_id"] = account_id
    st.session_state["viewing_select"] = account_id


def _on_viewing_change():
    _switch_to(st.session_state["viewing_select"])


def _open_client(account_id):
    _switch_to(account_id)
    st.session_state["page"] = "Dashboard"


def _back_to_clients():
    """The viewing bar's way out of a client's account."""
    _switch_to(st.session_state["user_id"])
    st.session_state["page"] = "Clients"


def _add_client():
    """Add client (Your clients): their name or household and, with an email,
    a setup link sent in the same step - from the advisor's name and firm,
    asked for here before the first invite if How clients see you is empty."""
    name = auth.clean_client_name(st.session_state.get("new_client_name"))
    email = (st.session_state.get("new_client_email") or "").strip()
    invite = bool(email) and st.session_state.get("new_client_invite", True)
    viewer = st.session_state["user_id"]
    if not name and not email:
        st.session_state["client_msg"] = (
            "error", "Enter their name, or a household name like Chen household.")
        return
    if email and "@" not in email:
        st.session_state["client_msg"] = (
            "error", "That doesn't look like an email address - check it, or leave it blank.")
        return
    c = connect(DB)
    try:
        p = prefs.load(c, viewer)
        card = p.get("advisor_card") or {}
        if invite and not card.get("name"):
            # the first invite: who it's from (saved as How clients see you)
            my_name = " ".join((st.session_state.get("new_adv_name") or "").split())[:60]
            my_firm = " ".join((st.session_state.get("new_adv_firm") or "").split())[:80]
            if not my_name:
                st.session_state["client_msg"] = (
                    "error", "Add your name first - the invite tells them who it's from.")
                return
            card = {**card, "name": my_name, **({"firm": my_firm} if my_firm else {})}
            p["advisor_card"] = card
            prefs.save(c, viewer, p)
        client_id = auth.create_client(c, viewer, email, name=name)
        shown = name or auth.get_username(c, client_id)   # an email is stored lower-cased
        sent = _send_invite(c, viewer, client_id) if invite else None
    except ValueError as exc:
        st.session_state["client_msg"] = ("error", str(exc).capitalize() + ".")
        return
    except DBError:
        st.session_state["client_msg"] = ("error", "That login is already taken - try another.")
        return
    finally:
        c.close()
    for k in ("new_client_name", "new_client_email"):
        st.session_state[k] = ""
    if sent is None:
        msg = (f"Added {shown}. " + ("When you're ready, send them a setup link from "
                                     "**Client login** in their account." if email else
                                     "Without an email, they can't sign in yet - you can add "
                                     "their statements and plan for them, or create a setup "
                                     "link to send yourself from **Client login**."))
        st.session_state["client_msg"] = ("success", msg, client_id)
    elif sent[0]:
        st.session_state["client_msg"] = ("success", f"Added {shown} and {sent[1][0].lower()}"
                                                     f"{sent[1][1:]}", client_id)
    else:
        st.session_state["client_msg"] = ("warning", f"Added {shown}, but {sent[1][0].lower()}"
                                                     f"{sent[1][1:]}", client_id)


def _send_invite(c, viewer, client_id):
    """Email one of this advisor's clients a fresh setup link, from the
    advisor's name and firm (the From line and the text). (sent, message)."""
    email = auth.email_status(c, client_id)["email"]
    if not email:
        return False, "This client has no email address yet."
    card = prefs.load(c, viewer).get("advisor_card") or {}
    if not card.get("name"):
        return False, ("Add your name under **Your clients > How clients see you** first - "
                       "the invite tells them who it's from.")
    token = auth.create_invite(c, viewer, client_id)   # ValueError: not their client
    body_name, from_name = _advisor_names(card, st.session_state["username"])
    if mailer.client_invite(email, f"{_app_address()}?invite={token}", body_name,
                            auth.INVITE_DAYS, from_name=from_name):
        return True, f"Sent {email} a setup link. It works for {auth.INVITE_DAYS} days."
    return False, ("the email couldn't be sent just now. Try again from **Client login** in "
                   "their account, or create a link there and send it yourself.")


def _set_client_password():
    pw = st.session_state.get("client_login_pw") or ""
    if len(pw) < auth.MIN_PASSWORD_LENGTH:
        st.session_state["login_msg"] = (
            "error", f"Use a password of at least {auth.MIN_PASSWORD_LENGTH} characters.")
        return
    viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
    c = connect(DB)
    try:
        if viewer == target or not auth.can_view(c, viewer, target):
            st.session_state["login_msg"] = ("error", "You can only set passwords for your clients.")
            return
        auth.set_password(c, auth.get_username(c, target), pw)
    finally:
        c.close()
    st.session_state["client_login_pw"] = ""
    st.session_state["login_msg"] = ("success", "Login password set - the client can log in now.")


def _create_invite():
    """A one-time setup link for the client being viewed. Only its hash is
    stored, so the link itself is shown once, here, for the advisor to send."""
    viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
    c = connect(DB)
    try:
        token = auth.create_invite(c, viewer, target)
    except ValueError:
        st.session_state["login_msg"] = ("error", "You can only invite your own clients.")
        return
    finally:
        c.close()
    st.session_state[f"invite_link_{target}"] = f"{_app_address()}?invite={token}"


def _email_invite():
    """Email the client being viewed a fresh setup link (ROADMAP G6): they
    choose a password, then answer the goals and risk questions."""
    viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
    c = connect(DB)
    try:
        sent, msg = _send_invite(c, viewer, target)
    except ValueError:
        sent, msg = False, "You can only invite your own clients."
    finally:
        c.close()
    if sent:
        st.session_state.pop(f"invite_link_{target}", None)
    st.session_state["login_msg"] = ("success" if sent else "error", msg[0].upper() + msg[1:])


def _cancel_invite():
    viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
    c = connect(DB)
    try:
        if viewer != target and auth.can_view(c, viewer, target):
            auth.cancel_invite(c, target)
    finally:
        c.close()
    st.session_state.pop(f"invite_link_{target}", None)
    st.session_state["login_msg"] = ("success", "Setup link cancelled - it no longer works.")


def _prepare_export():
    """Profile > Your data: the account's own data as a ZIP (export.py), kept
    in this session for the download button - built only when asked."""
    import export
    c = connect(DB)
    try:
        data = export.export_zip(c, st.session_state["user_id"])  # own account only
    finally:
        c.close()
    st.session_state["export_zip"] = (export.file_name(), data)


def _delete_my_holdings():
    if not st.session_state.get("confirm_delete_holdings"):
        return
    c = connect(DB)
    try:
        delete_holdings(c, st.session_state["user_id"])  # own account only
    finally:
        c.close()
    st.session_state["confirm_delete_holdings"] = False
    st.session_state["import_flash"] = "All your holdings were deleted."
    _after_import()


def _change_password():
    cur, new, again = (st.session_state.get(k) or "" for k in ("pw_current", "pw_new", "pw_again"))
    if new != again:
        st.session_state["pw_msg"] = ("error", "The new passwords don't match.")
        return
    c = connect(DB)
    try:
        # a stay-signed-in browser gets a fresh session; every other one ends
        result = auth.change_password(c, st.session_state["user_id"], cur, new,
                                      keep_session=bool(st.session_state.get("session_token")))
        stamp = auth.password_stamp(c, st.session_state["user_id"]) if result["ok"] else None
    finally:
        c.close()
    if not result["ok"]:
        st.session_state["pw_msg"] = ("error", result["error"])
        return
    for k in ("pw_current", "pw_new", "pw_again"):
        st.session_state[k] = ""
    st.session_state["pw_stamp"] = stamp  # keeps this tab signed in
    if result["token"]:
        st.session_state["session_token"] = result["token"]  # the cookie follows on this run
    st.session_state["pw_msg"] = ("success", "Password changed. Your other devices are signed out.")


def _open_holdings_dialog(kind):
    """Add holdings (the top bar's menu, and the pages' own buttons): the bar
    is drawn before this account's holdings are loaded, so it leaves a note
    and the dialog opens just after they are (see open_dialog below load())."""
    if kind == "manual":
        _manual_clear()  # start from the latest snapshot
    st.session_state["open_dialog"] = kind


PAGE = st.session_state["page"]
# the Money tab last open: the menu's Money goes back to it
if PAGE in MONEY_PAGES:
    st.session_state["money_tab"] = PAGE
# Keep where you are in the address, so a reload or a bookmark comes back here
# (read above, for a fresh session). The client is re-checked on every load.
_want_qp = {"page": _slug(PAGE), **({"client": str(USER_ID)} if USER_ID != LOGIN_ID else {})}
if dict(st.query_params) != _want_qp:
    st.query_params.from_dict(_want_qp)


def _nav_target(item):
    """The page a menu tab opens: Money its tab last open (Income at first)."""
    if item == MONEY:
        tab = st.session_state.get("money_tab")
        return tab if tab in MONEY_PAGES else MONEY_PAGES[0]
    return item


def _nav_current(item):
    """Whether this menu tab is the page showing (Money: any of its tabs)."""
    return PAGE in MONEY_PAGES if item == MONEY else PAGE == item


# The brand at the left of the top bar: the flag (Streamlit's own icon font,
# like the app's other icons), then the name; an advisor's phone shows the
# flag alone, to make room for Viewing.
_BRAND = (f"<div class='pt-brand{' pt-brand-compact' if IS_ADVISOR else ''}'>"
          "<span class='pt-brand-mark' aria-hidden='true' translate='no'>flag</span>"
          f"<span class='pt-brand-name'>{html.escape(APP_NAME)}</span></div>")
ACCOUNT_ICONS = {"Account": ":material/person:", "About": ":material/info:",
                 "Admin": ":material/admin_panel_settings:"}


def _render_viewing_pick():
    """An advisor's Viewing: their own portfolio or a client's, in the bar."""
    accounts_ = {LOGIN_ID: f"My portfolio ({st.session_state['username']})", **dict(CLIENTS)}
    st.session_state["viewing_select"] = USER_ID
    st.selectbox("Viewing", list(accounts_), format_func=accounts_.get, key="viewing_select",
                 on_change=_on_viewing_change, label_visibility="collapsed", width=210)


def _render_add_menu():
    """+ Add holdings: the ways to bring holdings in (a window each)."""
    with st.popover("Add holdings", icon=":material/add:", key="pt_add"):
        if ON_CLIENT:
            st.caption(f"Into **{_md_name(ACTIVE_NAME)}**'s account")
        st.button(":material/content_paste: Paste or type holdings", key="add_manual",
                  width="stretch", type="tertiary", on_click=_open_holdings_dialog,
                  args=("manual",),
                  help="Paste your positions from any brokerage's website, read them from "
                       "screenshots, type them in, or use percentages only.")
        st.button(":material/upload_file: Upload a CSV", key="add_import", width="stretch",
                  type="tertiary", on_click=_open_holdings_dialog, args=("import",),
                  help="A Positions export file from your brokerage.")
        if not HAS_HOLDINGS:
            st.button(":material/science: Try example data", key="add_sample", width="stretch",
                      type="tertiary", on_click=lambda: _load_sample(),  # defined further down
                      help="A made-up portfolio to explore with. Removed when you add your own.")


def _render_name_menu():
    """The name at the right: who's signed in, Account, About, Admin, the
    light / dark switch and Log out."""
    with st.popover(MY_NAME, icon=":material/account_circle:", type="tertiary", key="pt_me"):
        if IS_ADVISOR or IS_ADMIN:
            st.html(" ".join(f"<span class='pt-chip pt-role'>{r}</span>"
                             for r, on in (("Advisor", IS_ADVISOR), ("Admin", IS_ADMIN)) if on))
        _viewing = f" · viewing **{_md_name(ACTIVE_NAME)}**" if USER_ID != LOGIN_ID else ""
        st.caption(f"Logged in as **{MY_NAME}**{_viewing}")
        if IS_MANAGED_CLIENT:
            st.caption(f"Your advisor: **{_advisor_display_name()}**")
        if ADVISOR_REQUEST and ADVISOR_REQUEST["decision"] is None:
            st.caption(f":material/hourglass_top: **Advisor access requested** for "
                       f"{ADVISOR_REQUEST['firm']}. We're checking your details - usually "
                       "within two working days. Advisor tools appear once it's approved.")
        elif ADVISOR_REQUEST and ADVISOR_REQUEST["decision"] == "declined":
            st.caption("Your request for advisor access wasn't approved. Questions: "
                       f"{disclosures.CONTACT}")
        for p in ACCOUNT_MENU:
            st.button(f"{ACCOUNT_ICONS[p]} {_label(p)}", key=f"menu_{p}", on_click=_go, args=(p,),
                      width="stretch", type="primary" if PAGE == p else "tertiary")
        # flips light/dark in the browser (ui_enhancements.js); nothing runs here
        st.button(":material/contrast: Light / dark", key="pt_theme", type="tertiary",
                  width="stretch", help="Switch between the light and dark theme. System, Light "
                                        "and Dark are also in the ⋮ menu at the top right.")
        st.button(":material/logout: Log out", key="menu_logout", on_click=_logout,
                  width="stretch", type="tertiary")


def _render_top_bar():
    """The menu along the top: the brand, every tab, + Add holdings and the
    name menu. Pinned to the top of the window (the styles); on a phone the
    tabs step aside for the tab bar at the bottom (_render_tab_bar), and the
    two menus show as icons."""
    with st.container(horizontal=True, vertical_alignment="center", gap="small",
                      key="pt_topbar"):
        st.html(_BRAND, width="content")
        for item in NAV:
            if IS_ADVISOR and item == "Dashboard":
                _render_viewing_pick()   # Clients, then whose account, then its pages
            st.button(_nav_label(item), key=f"nav_{item}", on_click=_go,
                      args=(_nav_target(item),),
                      type="primary" if _nav_current(item) else "tertiary",
                      help=_label(item) if item in NAV_SHORT else None)
        if CAN_IMPORT:
            _render_add_menu()
        _render_name_menu()


# Phones: the same tabs along the bottom (the styles show it only on narrow
# screens), each with its icon above a short label.
TAB_ICONS = {"Get started": (":material/school:", "Learn"),
             "Dashboard": ((":material/pie_chart:", "Portfolio") if IS_ADVISOR
                           else (":material/home:", "Home")),
             "Plan": (":material/flag:", "Plan"), "AI Assistant": (":material/explore:", "Ask"),
             MONEY: (":material/payments:", "Money"),
             "Advisor notes": ((":material/sticky_note_2:", "Notes") if IS_ADVISOR
                               else (":material/support_agent:", "Advisor")),
             "Clients": (":material/groups:", "Clients")}


def _render_tab_bar():
    with st.container(horizontal=True, key="pt_tabbar"):
        for item in NAV:
            icon, short = TAB_ICONS[item]
            st.button(f"{icon} {short}", key=f"tab_{item}", on_click=_go,
                      args=(_nav_target(item),),
                      type="primary" if _nav_current(item) else "tertiary")


def _render_client_login():
    """An advisor in a client's account: the client's own login - a setup
    link to email or copy, or a password set for them."""
    link = st.session_state.get(f"invite_link_{USER_ID}")
    with st.popover("Client login", icon=":material/key:", type="tertiary",
                    key="pt_client_login"):
        msg = st.session_state.pop("login_msg", None)
        if msg:
            getattr(st, msg[0])(msg[1])
        c = connect(DB)
        try:
            pending = auth.pending_invite(c, USER_ID)
            client_email = auth.email_status(c, USER_ID)["email"]
            has_name = bool((prefs.load(c, LOGIN_ID).get("advisor_card") or {}).get("name"))
        finally:
            c.close()
        if client_email:
            st.button(f"Email {client_email} a setup link", key="invite_email",
                      type="primary", on_click=_email_invite, width="stretch",
                      disabled=not has_name,
                      help="They choose a password, then answer the goals and risk "
                           "questions - you'll see their answers.")
            if not has_name:
                st.caption("First add your name under **Your clients > How clients see "
                           "you** - the invite tells them who it's from.")
        if pending:  # (_fmt_date is defined further down)
            d = datetime.strptime(pending[:10], "%Y-%m-%d")
            until = f"{d:%b} {d.day}"
        st.caption(f"Send **{ACTIVE_NAME}** a setup link to choose their own password "
                   "and see their portfolio - you never need to know it.")
        if link and pending:
            st.code(link, language=None, wrap_lines=True)
            st.caption(f"Copy it and send it privately - it works once, until "
                       f"{until}. Anyone with the link can set "
                       "the password, so don't post it anywhere public.")
        elif pending:
            st.caption(f"A setup link is waiting to be used, until "
                       f"{until}. A new link replaces it.")
        st.button("Create a new setup link" if pending else "Create setup link",
                  key="invite_create", on_click=_create_invite, width="stretch",
                  type="secondary" if client_email else "primary",
                  help="A link to copy and send yourself.")
        if pending:
            st.button("Cancel the link", key="invite_cancel", on_click=_cancel_invite,
                      width="stretch", type="tertiary")
        st.markdown("**Or set a password yourself**")
        st.text_input("New password", type="password", key="client_login_pw")
        st.button("Set login password", on_click=_set_client_password, width="stretch")


_render_top_bar()
_render_tab_bar()  # phones only (see the styles); fixed to the bottom
# the menus' click-away and the theme switch, names for icon buttons, the
# current tab marked for screen readers, pull to refresh (see the file)
with open(os.path.join(HERE, "ui_enhancements.js"), encoding="utf-8") as _fh:
    st.html(f"<script>{_fh.read()}</script>", unsafe_allow_javascript=True)


def _save_login_pref(key, value):
    """Save one setting of the signed-in login (not the client being viewed),
    keeping the page's cached copy in step - a later settings save writes
    that copy back whole, and would otherwise undo this."""
    c = connect(DB)
    try:
        p = prefs.load(c, LOGIN_ID)
        p[key] = value
        prefs.save(c, LOGIN_ID, p)
    finally:
        c.close()
    cached = st.session_state.get("_prefs")
    if cached and cached[0] == LOGIN_ID:
        cached[1][key] = value


def _disclosures_seen():
    """Remember (per login) that this version of the disclosures was seen."""
    _save_login_pref("disclosures_seen", disclosures.LAST_UPDATED)
    st.session_state["disclosures_seen"] = disclosures.LAST_UPDATED


# An advisor inside a client's account always sees whose it is, at the top of
# every page, with the way back - so nobody edits the wrong person's plan.
# The client's own Get started and their login are here too: they belong to
# this client, not to the advisor's menu.
# (Not on Your clients itself: that page is about every client, not this one.)
if ON_CLIENT and PAGE != "Clients":
    with st.container(border=True, horizontal=True, vertical_alignment="center",
                      key="pt_viewing"):
        st.markdown(f":material/visibility: Viewing **{_md_name(ACTIVE_NAME)}**'s account",
                    width="stretch")
        if "Get started" in PAGES:
            st.button(_label("Get started"), key="viewing_start", icon=":material/route:",
                      type="primary" if PAGE == "Get started" else "tertiary",
                      on_click=_go, args=("Get started",))
        _render_client_login()
        st.button("Back to your clients", key="viewing_back", type="tertiary",
                  on_click=_back_to_clients)


# The disclosures promise to say when they change: once per new version, after
# sign-in (and once for everyone at first). Opening About counts as seen.
if "disclosures_seen" not in st.session_state:
    _dc = connect(DB)
    try:
        st.session_state["disclosures_seen"] = prefs.load(_dc, LOGIN_ID).get("disclosures_seen")
    finally:
        _dc.close()
if PAGE == "About" and st.session_state["disclosures_seen"] != disclosures.LAST_UPDATED:
    _disclosures_seen()
if st.session_state["disclosures_seen"] != disclosures.LAST_UPDATED:
    with st.container(border=True, horizontal=True, vertical_alignment="center"):
        st.markdown(f":material/info: **About and disclosures** - what {APP_NAME} is, how your "
                    "data is used and what's sent to the AI - "
                    + ("was updated on " + disclosures.LAST_UPDATED + "."
                       if st.session_state["disclosures_seen"] else "worth a quick read."),
                    width="stretch")
        st.button("Read it", key="disc_read", on_click=lambda: (_disclosures_seen(), _go("About")))
        st.button("Got it", key="disc_ok", type="tertiary", on_click=_disclosures_seen)


def _resend_confirmation():
    sent, note = _send_confirmation(LOGIN_ID)
    st.session_state["email_flash"] = (sent, note)


# A self-serve account's email waits to be confirmed (auth.confirm_email): a
# notice with "Send it again" until it is. Looked up each run only while
# waiting - the link may be opened in another tab - then remembered.
if st.session_state.get("email_state") is None:
    if _gate is not None:   # the row the two-step gate read at the top of this run
        _es = auth.email_status_of(_gate[1])
    else:
        _ec = connect(DB)
        try:
            _es = auth.email_status(_ec, LOGIN_ID)
        finally:
            _ec.close()
    if not _es["email"] or _es["confirmed"]:
        st.session_state["email_state"] = "done"
    _waiting_email = None if st.session_state.get("email_state") else _es["email"]
else:
    _waiting_email = None
_email_flash = st.session_state.pop("email_flash", None)
if _email_flash:
    (st.success if _email_flash[0] else st.warning)(_email_flash[1])
if _waiting_email and st.session_state.get("email_brief"):
    with st.container(horizontal=True, vertical_alignment="center", gap="small",
                      key="pt_email_brief"):
        st.caption(f":material/mail: We've sent a link to {_waiting_email} - confirm any time.",
                   width="content")
        st.button("Send it again", key="email_resend", type="tertiary",
                  on_click=_resend_confirmation)
elif _waiting_email:
    with st.container(border=True, horizontal=True, vertical_alignment="center"):
        st.markdown(f":material/mail: **Confirm your email** - open the link we sent to "
                    f"{_waiting_email}. It unlocks Ask {GUIDE} and the other AI features, and "
                    "lets you reset your password if you forget it.", width="stretch")
        st.button("Send it again", key="email_resend", on_click=_resend_confirmation)


def _anthropic_key() -> str | None:
    """Same resolution order as resolve_key() uses for FINNHUB_API_KEY -
    .env locally, then the OS environment (which is how Streamlit
    Community Cloud exposes its Secrets UI entries). None if unset -
    every AI-assisted-parsing call site treats that as "skip the AI
    fallback, strict parsing only," today's exact behavior."""
    return (load_env(ENV_PATH).get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or "").strip() or None


def _ai_status(kind, *, full_run=False):
    """This month's AI allowance for `kind` (ai_usage.py) of whoever is signed
    in - an advisor in a client's account uses their own. `full_run`: the
    caller is drawn in the page's full run (not in a fragment or a window), so
    the login's row the two-step gate read at the top of this run is used
    rather than read again; the count used so far is always read."""
    gate = _gate_read(LOGIN_ID) if full_run else None
    c = connect(DB)
    try:
        return ai_usage.status(c, LOGIN_ID, kind, user=gate[1] if gate else None)
    finally:
        c.close()


def _ai_record(kind):
    """Count one AI request against the signed-in account - only once it has
    succeeded, so a failed one doesn't use up the month's allowance."""
    c = connect(DB)
    try:
        ai_usage.record(c, LOGIN_ID, kind)
    finally:
        c.close()


def _ai_failed(exc, kind, feature=""):
    """An AI request (`kind`, ai_usage.LIMITS) failed: the details go to the
    server log, a key or set-up problem is noted for the admin like any other
    error (error_alerts.py: its type and place only), and what to show comes
    back - one calm sentence per kind of failure, never the error's text."""
    ai_usage.log_failure(exc, kind)
    if ai_usage.failure_kind(exc) == ai_usage.UNAVAILABLE:
        try:
            import error_alerts
            error_alerts.report(DB, exc, copy="Staging" if STAGING else "Live",
                                send=pgcompat.is_postgres_dsn(DB))
        except Exception:
            pass
    return ai_usage.failure_text(exc, GUIDE, feature)


CHAT_MESSAGE_LIMIT = 40  # per conversation - keeps each one a sensible length
QUICK_STARTS = {
    "Help me get started": "I'm new to investing. Help me figure out how to get started.",
    "Review my portfolio": "Review my current portfolio against my goals and suggest improvements.",
    "Check for overlap and concentration": "Check my holdings for overlap between funds and "
                                           "for anything I'm too concentrated in.",
}
# ...and before anything is invested: nothing to review yet
QUICK_STARTS_NEW = {
    "Help me get started": QUICK_STARTS["Help me get started"],
    "What should I do before I invest?": "I haven't started investing yet. Looking at my "
                                         "situation, what do people usually take care of "
                                         "first, and in what order?",
    "Which account type fits me?": "What's the difference between a regular brokerage "
                                   "account, a Roth IRA and a 401(k)? Which questions should I "
                                   "ask myself to pick one?",
}


def _legacy_prefs_path(account_id):
    return os.path.join(HERE, f".dashboard_prefs.{account_id}.json")


def _rules_for(account_id, conn=None):
    """That account's saved alert limits, else defaults."""
    c = conn or connect(DB)
    try:
        saved = prefs.load(c, account_id, _legacy_prefs_path(account_id))
    finally:
        if conn is None:
            c.close()
    return _rules_from(saved)


def _rules_from(saved_prefs):
    """The alert limits in an account's settings (prefs.load), else defaults."""
    saved = saved_prefs.get("rules") or {}
    if not isinstance(saved, dict):
        saved = {}
    return [{**r, "abs_gt": float(saved.get(r["key"], r["abs_gt"]))} for r in alerts.DEFAULT_RULES]


_view("clients")


_view("meeting")


_view("reports")


_view("admin")

# the signed-in person's own account (name, email, password, data)
_view("account")


_view("profile")


_view("plan")


_view("proposals")


# milestones and gear: the "milestone reached" window and Your kit (gear.py)
_view("kit")

# Fee check: each fund's yearly fee in dollars, in a window (fees.py)
_view("fees")

# Fund overlap: do the funds hold the same companies? (fund_holdings.py)
_view("fund_overlap")

# a new investor's first steps, one screen at a time (Get started shows it)
_view("first_steps")
_view("get_started")


_view("assistant")


MASK = "•••"


def _hidden() -> bool:
    return bool(st.session_state.get("hide_amounts", False))


def mask_or(s):
    """MASK when 'hide amounts' is on, else `s` unchanged."""
    return MASK if _hidden() else s


def _blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


# --------------------------------------------------------------------------- #
def fmt_money(v):
    if _hidden():
        return MASK
    if _blank(v):
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def fmt_pct(v, signed=True):
    """A percent. Signed ("+0.87%") for a change - a day's move, a gain, a
    return; signed=False ("0.87%") for a level - a yield, a share of the
    portfolio (fmt_pct_level, the metrics' "pct_level")."""
    if _hidden():
        return MASK
    return "—" if _blank(v) else (f"{v:+.2f}%" if signed else f"{v:.2f}%")


def fmt_pct_level(v):
    return fmt_pct(v, signed=False)


def color_sign(v):
    if _hidden() or v is None or pd.isna(v) or v == 0:
        return ""
    up, down = SIGN_COLORS["dark" if st.context.theme.type == "dark" else "light"]
    return f"color: {up if v > 0 else down}; font-weight: 600"


def fmt_price(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"${v:,.2f}")


def fmt_qty(v):
    return "—" if _blank(v) else f"{v:,.4f}".rstrip("0").rstrip(".")


def fmt_num(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"{v:,.2f}")


def fmt_int(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"{v:,.0f}")


FORMATTERS = {"money": fmt_money, "pct": fmt_pct, "pct_level": fmt_pct_level,
              "price": fmt_price, "qty": fmt_qty, "num": fmt_num, "int": fmt_int}

# axis / tooltip number format string per metric-format name
AXIS_FORMAT = {"money": charts.MONEY_AXIS, "price": "$,.2f", "pct": ".2f", "pct_level": ".2f",
               "int": "d", "num": ",.2f"}
TOOLTIP_FORMAT = {"money": "$,.2f", "price": "$,.2f", "pct": ".2f", "pct_level": ".2f",
                  "int": "d", "num": ",.2f"}


def _stat_tiles(ctx, keys, ncols=4):
    """A compact label/value grid for a list of metrics.py keys — the
    Robinhood-style "stats" block under a ticker's chart. Skips keys with no
    registered metric; renders '—' for a None value like the Holdings table."""
    keys = [k for k in keys if k in M.BY_KEY]
    if not keys:
        return
    # one row of columns per ncols stats, so a phone (where columns stack;
    # two per line there, .st-key-pt_stat_tiles) keeps the reading order
    with st.container(key="pt_stat_tiles"):
        for start in range(0, len(keys), ncols):
            for col, k in zip(st.columns(ncols), keys[start:start + ncols]):
                m = M.BY_KEY[k]
                v = M.value(k, ctx)
                text = FORMATTERS[m.fmt](v) if m.fmt in FORMATTERS else ("—" if _blank(v) else str(v))
                with col:
                    st.caption(m.label)
                    if m.color_sign and not _hidden() and not _blank(v) and v != 0:
                        st.markdown(f"<span class='{'pt-up' if v > 0 else 'pt-down'}' "
                                    f"style='font-weight:600'>{text}</span>", unsafe_allow_html=True)
                    else:
                        st.markdown(f"**{text}**")


# Categorical palette (dataviz reference palette, fixed slot order), light and
# dark steps. Asset types keep a fixed slot so a color always means the same
# thing; anything else takes the next free slot.
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
SERIES_OTHER = "#8a8a86"
ASSET_SLOT = {"Equity": 0, "ETF / CEF": 1, "Cash": 2, "Fixed Income": 3, "Mutual Funds": 4,
              "Option": 6}
# asset classes keep the colors of their nearest broker type
CLASS_SLOT = {"Stocks": 0, "Bonds": 3, "Cash": 2, "Other": 6}


def _slot_map(labels, fixed=None):
    """{label: palette slot}. Labels in `fixed` keep their slot; the rest take
    the unused slots in name order - so a color follows its entity, not its
    rank. Past eight, labels get the neutral 'other' gray (slot None)."""
    fixed = fixed or {}
    out = {lbl: fixed[lbl] for lbl in labels if lbl in fixed}
    free = [i for i in range(len(SERIES_LIGHT)) if i not in out.values()]
    for lbl in sorted(lbl for lbl in labels if lbl not in out):
        out[lbl] = free.pop(0) if free else None
    return out


def _alloc_bar(rows, title, slots):
    """Part-to-whole as one stacked bar plus a legend of label / % / value.
    HTML rather than a chart library so it lays out cleanly at phone width;
    every segment is named in the legend, so identity never rests on color."""
    import html as _h
    colors = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT

    def color(label):
        i = slots.get(label)
        return colors[i] if i is not None else SERIES_OTHER

    segs = "".join(
        f"<div class='pt-alloc-seg' style='flex:{r['value']} 0 0;background:{color(r['label'])}' "
        f"title='{_h.escape(r['label'], quote=True)}'></div>"
        for r in rows if (r["value"] or 0) > 0)
    legend = ""
    for r in rows:
        pct = MASK if _hidden() or r["pct"] is None else f"{r['pct']:.1f}%"
        legend += ("<div class='pt-legend-row'>"
                   f"<span class='pt-swatch' style='background:{color(r['label'])}'></span>"
                   f"<span class='pt-legend-label'>{_h.escape(r['label'])}</span>"
                   f"<span class='pt-legend-pct'>{pct}</span>"
                   f"<span class='pt-legend-val'>{fmt_money(r['value'])}</span></div>")
    return (f"<div class='pt-alloc-title'>{_h.escape(title)}</div>"
            f"<div class='pt-alloc-bar' aria-hidden='true'>{segs}</div>"
            f"<div class='pt-legend'>{legend}</div>")


def _account_mix(by_account, positions, cash_by_account, slots, group="by_asset_class"):
    """'By account': each account's share of the portfolio, with a thin bar
    of its own asset mix underneath - one view instead of a by-account bar
    plus a separate asset-mix chart per account. Mix colors match the asset
    type legend beside it; each segment names itself on hover."""
    import html as _h
    colors = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
    out = "<div class='pt-alloc-title'>By account</div>"
    for r in by_account:
        acct = r["label"]
        mix = allocate([p for p in positions if p["account"] == acct],
                       {acct: cash_by_account.get(acct, 0.0)}, CLASS_SPLITS)[group]
        segs, tips = "", []
        for m in mix:
            if (m["value"] or 0) <= 0:
                continue
            i = slots.get(m["label"])
            tip = m["label"] if _hidden() or m["pct"] is None else f"{m['label']} {m['pct']:.1f}%"
            tips.append(tip)
            segs += (f"<div class='pt-alloc-seg' style='flex:{m['value']} 0 0;"
                     f"background:{colors[i] if i is not None else SERIES_OTHER}' "
                     f"title='{_h.escape(tip, quote=True)}'></div>")
        pct = MASK if _hidden() or r["pct"] is None else f"{r['pct']:.1f}%"
        out += ("<div class='pt-acct'><div class='pt-legend-row'>"
                f"<span class='pt-legend-label'>{_h.escape(acct)}</span>"
                f"<span class='pt-legend-pct'>{pct}</span>"
                f"<span class='pt-legend-val'>{fmt_money(r['value'])}</span></div>"
                # the mix is only in the segments' tooltips, so say it for screen readers
                f"<div class='pt-alloc-bar pt-mini' role='img' aria-label="
                f"'{_h.escape(acct + ' mix: ' + ', '.join(tips), quote=True)}'>{segs}</div></div>")
    return out


def _render_classification(positions):
    """How each holding is classed (Stocks / Bonds / Cash / Other) and where
    that came from; whoever manages the account can set a holding by hand."""
    by_sym = {p["symbol"]: p for p in positions if p.get("symbol")}
    if not by_sym:
        return
    rows = []
    for s in sorted(by_sym):
        split, source = asset_classes.split_for(s, by_sym[s].get("asset_type"), sec_info.get(s),
                                                CLASS_OVERRIDES)
        rows.append({"Symbol": s, "Holds": asset_classes.describe(split),
                     "From": asset_classes.SOURCE_LABELS[source],
                     "Set to": CLASS_OVERRIDES.get(s, "Automatic")})
    n_guess = sum(r["From"] == asset_classes.SOURCE_LABELS["broker"] for r in rows)
    with st.expander("How holdings are classified"
                     + (f" · {n_guess} from broker type only" if n_guess else "")):
        st.caption("Funds are split by what they hold, from Yahoo - a balanced fund counts part "
                   "stocks, part bonds. Without Yahoo data a holding goes by its broker type "
                   "(Equity is stocks, Fixed Income is bonds); the rest fills in by itself."
                   + (" Choose a holding below to decide its class yourself." if CAN_MANAGE
                      else ""))
        df = pd.DataFrame(rows)
        st.dataframe(df if CAN_MANAGE else df.drop(columns=["Set to"]), hide_index=True,
                     width="stretch")
        if not CAN_MANAGE:
            return
        with st.form("class_override_form", border=False):
            c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
            sym = c1.selectbox("Holding", sorted(by_sym), key="class_pick_symbol")
            cls = c2.selectbox("Set to", ["Automatic", *asset_classes.CLASSES],
                               key="class_pick_class",
                               help="Automatic uses Yahoo, else the broker type.")
            if c3.form_submit_button("Save", width="stretch"):
                new = dict(CLASS_OVERRIDES)
                if cls in asset_classes.CLASSES:
                    new[sym] = cls
                else:
                    new.pop(sym, None)
                p = _read_prefs()
                p[asset_classes.OVERRIDES_PREF] = new  # kept for symbols not held right now
                _write_prefs(p)
                st.rerun()


def _read_prefs():
    """This account's saved settings (prefs.py). Read from the database once
    per browser session, then served from session state - the page reads a
    setting many times per run."""
    cached = st.session_state.get("_prefs")
    if cached is None or cached[0] != USER_ID:
        conn = connect(DB)
        try:
            cached = (USER_ID, prefs.load(conn, USER_ID, PREFS_PATH))
        finally:
            conn.close()
        st.session_state["_prefs"] = cached
    return dict(cached[1])


def _write_prefs(d):
    conn = connect(DB)
    try:
        prefs.save(conn, USER_ID, d)
    finally:
        conn.close()
    st.session_state["_prefs"] = (USER_ID, dict(d))


def load_columns():
    saved = _read_prefs().get("columns")
    keys = [k for k in (saved or M.DEFAULT_KEYS) if k in M.BY_KEY and M.BY_KEY[k].available]
    return keys or list(M.DEFAULT_KEYS)


def save_columns(keys):
    p = _read_prefs()
    p["columns"] = list(keys)
    _write_prefs(p)


def load_rules():
    saved = _read_prefs().get("rules") or {}
    return [{**r, "abs_gt": float(saved.get(r["key"], r["abs_gt"]))} for r in alerts.DEFAULT_RULES]


def save_rules(rules):
    p = _read_prefs()
    p["rules"] = {r["key"]: r["abs_gt"] for r in rules}
    _write_prefs(p)


def load_perf_series():
    s = _read_prefs().get("perf_series")
    return s if s in perf.SERIES_LABEL else perf.SERIES[0][0]


DEFAULT_DRIFT_THRESHOLD = 5.0  # percentage points off target before flagging


def load_plan():
    """This account's plan (plans.py), or None. Cached for the session like
    the settings; saving through save_plan_fields() refreshes it."""
    cached = st.session_state.get("_plan")
    if cached is None or cached[0] != USER_ID:
        conn = connect(DB)
        try:
            cached = (USER_ID, plans.get_plan(conn, USER_ID))
        finally:
            conn.close()
        st.session_state["_plan"] = cached
    return cached[1]


def save_plan_fields(fields: dict):
    """Save into this account's plan, recording who saved it (an advisor
    editing a client's plan is recorded as the advisor)."""
    conn = connect(DB)
    try:
        plan = plans.save_plan(conn, USER_ID, fields, set_by=LOGIN_ID)
    finally:
        conn.close()
    st.session_state["_plan"] = (USER_ID, plan)
    return plan


# Read once per run: what several parts of one page need. This script runs
# afresh on every rerun, so it starts empty each time (a click's callback has
# already saved its change). A fragment's or window's own rerun still sees the
# last full run's, so keep here only what changes by a full rerun - never what
# a fragment itself saves (the Plan tabs read their own).
_RUN = {}
# each holding's asset-class split (asset_classes.py), worked out once the
# holdings are loaded below; empty until then, so the pages drawn before
# anything is brought in (Ask Northwend, meeting prep) can use it too
CLASS_SPLITS = {}


def _profile():
    """This account's investor profile (advisor.get_profile), read once per
    run - Home's route card and kit, Get started, the first steps and the map
    plate above the title all use it. Saving it (the profile form, then
    st.rerun; a first steps button's callback) starts a new run."""
    if "profile" not in _RUN:
        import advisor
        conn = connect(DB)
        try:
            _RUN["profile"] = advisor.get_profile(conn, USER_ID)
        finally:
            conn.close()
    return dict(_RUN["profile"])


def load_alloc_targets():
    """{asset-type label: target %}, from the plan's target mix. Only labels
    with a nonzero target are included — an unset label has no target and is
    never flagged, rather than implicitly meaning "target 0%". Targets saved
    before plans existed (in the settings) are used until the plan has some."""
    plan = load_plan()
    saved = (plan or {}).get("target_alloc")
    if not saved:  # the pre-plans settings key, in the old broker-type groups
        saved, _cleared = asset_classes.convert_targets(_read_prefs().get("alloc_targets"))
    return {k: float(v) for k, v in (saved or {}).items() if v}


def save_alloc_targets(targets: dict):
    save_plan_fields({"target_alloc": {k: v for k, v in targets.items() if v}})


def load_drift_threshold():
    v = _read_prefs().get("drift_threshold")
    return float(v) if v else DEFAULT_DRIFT_THRESHOLD


def save_drift_threshold(v):
    p = _read_prefs()
    p["drift_threshold"] = float(v)
    _write_prefs(p)


def save_hide(on):
    p = _read_prefs()
    p["hide_amounts"] = bool(on)
    _write_prefs(p)


def save_perf_series(col):
    p = _read_prefs()
    p["perf_series"] = col
    _write_prefs(p)


def load(conn):
    """Return (snapshot_date, positions, cash_by_account, quotes, watchlist
    tickers), read on `conn`. The snapshot is the one found at the top of
    this run (holdings are only saved in a callback or a window, each
    followed by a new run)."""
    snap = _LATEST_SNAPSHOT
    import overview
    if not snap:
        # nothing brought in yet - the watchlist still works (Learn's example
        # funds go on it before anything is bought)
        watch = watchlist.list_tickers(conn, USER_ID) if PAGE == "Watchlist" else []
        return None, [], {}, overview.latest_quotes(conn, sorted(watch)) if watch else {}, watch
    rows = conn.execute(
        "SELECT * FROM positions WHERE snapshot_date = ? AND user_id = ? ORDER BY account, symbol",
        (snap, USER_ID)
    ).fetchall()
    cash_by_account = {
        r["account"]: r["cash_value"] or 0.0
        for r in conn.execute(
            "SELECT account, cash_value FROM account_totals WHERE snapshot_date = ? AND user_id = ?",
            (snap, USER_ID))
    }
    # the latest quote of each ticker this account holds or watches - not
    # every ticker in price_history, which grows every minute
    watch = watchlist.list_tickers(conn, USER_ID)   # read once: the Watchlist uses it too
    quotes = overview.latest_quotes(conn, sorted({r["symbol"] for r in rows} | set(watch)))
    # Nicknames replace the broker's account names from here on (display
    # only); the broker's name stays available as "broker_account".
    positions = [dict(r) for r in rows]
    for p in positions:
        p["broker_account"] = p["account"]
        p["account"] = accounts.display(p["account"], ACCOUNT_LABELS)
    cash_by_account = {accounts.display(a, ACCOUNT_LABELS): v for a, v in cash_by_account.items()}
    return snap, positions, cash_by_account, quotes, watch


AUTO_REFRESH_AFTER = timedelta(minutes=15)


# ---- header -------------------------------------------------------------- #
def _sync_history(tickers=None, *, quick=False):
    """Pull Yahoo history, then rerun to show it. `quick` is the automatic
    backfill for holdings that have none yet: daily bars and fundamentals
    only (seconds, not minutes) - the full sync and the nightly job add the
    intraday bars. It stays silent when it can't run."""
    try:
        import sync_history
    except ImportError:
        if quick:
            return
        st.session_state["refresh_msg"] = ("error", "yfinance not installed — run: pip install yfinance")
        st.rerun()
    prog = st.progress(0.0, text="Loading price history for your holdings…" if quick
                       else "Contacting Yahoo…")
    try:
        summary = sync_history.sync(
            DB, tickers, period=sync_history.DEFAULT_PERIOD, with_intraday=not quick,
            on_progress=lambda i, n, tk, nr, ok, err: prog.progress(
                i / n, text=f"{tk} ({i}/{n}) — {nr:,} rows"),
        )
    except Exception:  # noqa: BLE001 - the automatic backfill must never break the page
        if not quick:
            raise
        prog.empty()
        return
    prog.empty()
    if quick:
        if summary["ok"]:
            st.session_state["refresh_msg"] = (
                "toast", f"Loaded price history for {summary['ok']} holding(s).")
            st.rerun()
        return
    msg = (f"Synced {summary['bars_written']:,} daily + "
           f"{summary['intraday_written']:,} intraday bars for "
           f"{summary['ok']}/{summary['tickers']} tickers.")
    if summary["failed"]:
        st.session_state["refresh_msg"] = ("warning", msg + " No data for: " + ", ".join(summary["failed"]))
    else:
        st.session_state["refresh_msg"] = ("success", msg)
    st.rerun()


def _local_time(ts):
    """A stored UTC timestamp as an aware datetime in the viewer's timezone
    (UTC when the browser didn't report one)."""
    at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    off = st.context.timezone_offset
    return at.astimezone(timezone(-timedelta(minutes=off)) if off is not None else timezone.utc)


def _fmt_when(ts):
    """'4:18 PM' today, 'Sep 28, 4:18 PM' this year, with the year otherwise."""
    try:
        at = _local_time(ts)
    except (TypeError, ValueError):
        return str(ts)
    now = datetime.now(at.tzinfo)
    clock = at.strftime("%I:%M %p").lstrip("0") + ("" if st.context.timezone_offset is not None
                                                   else " UTC")
    if at.date() == now.date():
        return clock
    if at.year == now.year:
        return f"{at:%b} {at.day}, {clock}"
    return f"{at:%b} {at.day}, {at.year}, {clock}"


def _fmt_date(d):
    """'Jan 15, 2026' from '2026-01-15'."""
    try:
        d = datetime.strptime(str(d)[:10], "%Y-%m-%d")
    except ValueError:
        return str(d)
    return f"{d:%b} {d.day}, {d.year}"


_view("holdings_input")


_view("start_home")


def _toggle_hide():
    st.session_state["hide_amounts"] = not st.session_state.get("hide_amounts", False)
    save_hide(st.session_state["hide_amounts"])


@st.fragment(run_every=LIVE_EVERY_SEC)
def _live_status():
    """Keeps prices current without a Refresh button: every minute (only this
    line reruns) it fetches whatever quotes are due (live_prices.freshen -
    shared across everyone viewing, so each ticker is asked for at most once a
    minute), then redraws the page if any price changed. It waits while a
    dialog is open, so it never interrupts adding holdings."""
    ss = st.session_state
    try:
        c = connect(DB)
        try:
            # what this page run loaded (holdings and the watchlist only change
            # by a save, which starts a new run) - not read again every minute
            live = live_prices.freshen(
                c, USER_ID, resolve_key(None, ENV_PATH),
                known=(snapshot, {p["symbol"]: p["asset_type"] for p in positions},
                       list(watch_tickers)))
        finally:
            c.close()
    except Exception:  # noqa: BLE001 - prices failing must never break the page
        live = {"updated": 0, "as_of": None, "live": False}
    if (live["updated"] or (PAGE == "Watchlist" and live.get("watch_fetched"))) \
            and not ss.get("dialog_open"):
        st.rerun()  # the whole page, with the new prices
    as_of = live["as_of"] or live_prices._parse(last_live)
    if live["live"] and as_of:
        age = (datetime.now(timezone.utc) - as_of).total_seconds()
        ago = ("just now" if age < 90 else f"{int(age // 60)} min ago" if age < 3600
               else _fmt_when(as_of))
        prices = f"<span class='pt-live' aria-hidden='true'>●</span> Live · prices updated {ago}"
    elif as_of:
        prices = f"Market closed · prices as of {_fmt_when(as_of)}"
    else:
        prices = "No live prices yet"
    if n_live < len(positions):
        prices += f" ({n_live} of {len(positions)} priced)"
    _what = {manual_entry.SOURCE: "Entered by hand", manual_entry.PCT_SOURCE: "Percentages",
             SAMPLE_SOURCE: "Example portfolio"}.get(SNAPSHOT_SOURCE, "Statement")
    st.html(f"<div class='pt-status'>{prices}"
            # the watchlist before anything is brought in: no holdings to date
            + (f" · {_what} from {_fmt_date(snapshot)}" if snapshot else "") + "</div>")


def _expedition_eyebrow():
    """The map plate above an investor's Home and Get started title: where
    they are on the route (route.region) - "Learner's ridge · your expedition"."""
    state = _route_state(HAS_HOLDINGS)
    waypoints = state["waypoints"]   # their route (views/get_started.py)
    here, _next = route.region(waypoints)
    if PAGE == "Get started":
        n = sum(d for _, _, d in waypoints)
        return f"{here} · {n} of {len(waypoints)} waypoints reached"
    return f"{here} · your expedition"


def _money_tabs():
    """Money's tabs: Income, Activity and Watchlist, a page each (?page=income
    and so on), so links to any of them keep working."""
    with st.container(horizontal=True, gap="small", key="pt_money_tabs"):
        for p in MONEY_PAGES:
            st.button(_label(p), key=f"money_{p}", on_click=_go, args=(p,),
                      type="primary" if PAGE == p else "tertiary")


def _page_header(title, *, data=True):
    """The page's title with the hide-amounts toggle and, on `data` pages
    (this account's portfolio), a status line that keeps prices current by
    itself (_live_status) - there's no Refresh button. Adding or updating
    holdings is the top bar's + Add holdings. An investor's Home and
    Get started carry the map plate above the title (_expedition_eyebrow).
    Income, Activity and Watchlist are the Money page's tabs: their title is
    Money, with the tabs under it (_money_tabs)."""
    if PAGE in MONEY_PAGES:
        title = MONEY
    # (an advisor's client's Home is their advisor's next step, not an expedition)
    if INVESTOR_VIEW and not IS_ADVISOR and (PAGE == "Get started"
                                             or PAGE == "Dashboard" and not CLIENT_MODE):
        st.html(f"<div class='pt-eyebrow'>{html.escape(_expedition_eyebrow())}</div>")
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.title(title, anchor=False, width="stretch")
        # Learn (and first steps, shown in its place) has nothing of theirs to
        # hide: examples and practice money only. The setting still holds.
        if PAGE != "Get started":
            st.button(":material/visibility_off:" if _hidden() else ":material/visibility:",
                      key="pt_hide", type="tertiary", on_click=_toggle_hide,
                      help="Show amounts" if _hidden() else "Hide amounts - mask every dollar "
                                                             "and percent with " + MASK)
    if PAGE in MONEY_PAGES:
        _money_tabs()
    if data:
        _live_status()
        if SNAPSHOT_SOURCE == SAMPLE_SOURCE:
            with st.container(border=True, horizontal=True, vertical_alignment="center"):
                st.markdown(":material/science: **This is an example portfolio** - made-up "
                            "holdings to explore with. It's removed as soon as you import or "
                            "enter your own.", width="stretch")
                if CAN_IMPORT:
                    st.button("Remove example", key="pt_clear_sample", on_click=_clear_sample)
        elif SNAPSHOT_SOURCE == manual_entry.PCT_SOURCE:
            st.caption(":material/percent: A percentages portfolio - dollar amounts are pretend, "
                       "scaled to the total you chose. Change it with **Add holdings** at the top: "
                       "**Paste or type holdings**.")

    # the result of a refresh / sync / import that happened just before the rerun
    _msg = st.session_state.pop("refresh_msg", None)
    if _msg:
        getattr(st, _msg[0])(_msg[1])
    _flash = st.session_state.pop("import_flash", None)
    if _flash:
        st.success(_flash)


def _signed_money(v):
    """'+$12.30' / '-$12.30' (fmt_money has no plus sign)."""
    if _hidden():
        return MASK
    if _blank(v):
        return "—"
    return ("+" if v > 0 else "") + fmt_money(v)


def _tone(v, html):
    """Wrap `html` in the gain/loss color for `v` (plain when hidden or flat)."""
    if _hidden() or _blank(v) or v == 0:
        return html
    return f"<span class='{'pt-up' if v > 0 else 'pt-down'}'>{html}</span>"


# ---- calm by default (ROADMAP S6): a summary first, detail in a window ---- #
def _show_everything():
    """True for the full pages: advisors always, and an investor who turned on
    Show everything on the Account page. Otherwise Income, Activity, Watchlist
    and Ask Northwend lead with a short summary and open the detail in a window."""
    return IS_ADVISOR or bool(_read_prefs().get("show_everything"))


_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}\b)")


def _stat_row(markup):
    """A row of stat boxes (.pt-stats) with a line-break chance after each
    thousands comma: on a phone a box can be about 90px wide, so a long
    amount goes onto two lines at a comma instead of being cut off (the
    phone styles let .pt-stat-value wrap; on a wider screen it fits)."""
    return _THOUSANDS_COMMA.sub(",<wbr>", markup)


def _summary_stats(items):
    """A row of small stat boxes (.pt-stats): [(label, value_html, sub_html or None)].
    Values are already formatted (and masked) by the caller. A list to screen
    readers, one item per box, so each reads as its label then its value."""
    st.html(_stat_row(
            "<div class='pt-stats' role='list' aria-label='Summary' style='margin-top:.25rem'>"
            + "".join(
                "<div class='pt-stat' role='listitem'>"
                f"<div class='pt-stat-label'>{html.escape(label)}</div>"
                f"<div class='pt-stat-value'>{value}</div>"
                + (f"<div class='pt-stat-sub'>{sub}</div>" if sub else "") + "</div>"
                for label, value, sub in items) + "</div>"))


def _next_step_card(key, line, button=None):
    """One next step under a page's summary, in the Fee check card's look:
    `line` is HTML; `button` is (label, on_click or None, args) or None.
    Returns whether the button was pressed (to open a window with it)."""
    with st.container(border=True, horizontal=True, vertical_alignment="center",
                      key=f"pt_next_{key}"):
        # one sentence to a screen reader: "Next step: ..." (the button's icon
        # is left out of its name by ui_enhancements.js)
        st.html("<div class='pt-route-label'>Next step<span class='pt-sr'>:</span></div>"
                f"<div class='pt-region'>{line}</div>",
                width="stretch")
        if button:
            label, on_click, args = button
            return st.button(label, key=f"next_{key}", type="tertiary", on_click=on_click,
                             args=args)
    return False


def _open_window(window, *args):
    """Open a detail window (an st.dialog); live prices wait while it's open."""
    st.session_state["dialog_open"] = True
    window(*args)


def _detail_tiles(tiles):
    """Summary tiles in a row, each opening its detail in a window:
    [(key, icon, title, summary, button label, window, args)]."""
    if not tiles:
        return
    cols = st.columns(len(tiles))
    for col, (key, icon, title, summary, label, window, args) in zip(cols, tiles):
        with col.container(border=True, key=f"pt_tile_{key}"):
            st.markdown(f"{icon} **{title}**")
            st.caption(summary.replace("$", "\\$"))   # two amounts would read as math
            if st.button(label, key=f"tile_{key}", type="tertiary",
                         icon=":material/open_in_new:"):
                _open_window(window, *args)


def _ask_guide(question):
    """A button's "Ask Northwend": carry the question over to the chat."""
    st.session_state["coach_prompt"] = question
    st.session_state["page"] = "AI Assistant"


def _calm_footer():
    st.caption("Prefer everything on one page? Turn on **Show everything** on the Account page.")


# --------------------------------------------------------------------------- #
if not pgcompat.is_postgres_dsn(DB) and not os.path.isfile(DB):
    st.error("No `portfolio.db` yet. Build it first:")
    st.code("python portfolio.py import \"path\\to\\All-Accounts-Positions-....csv\"", language="bash")
    st.stop()

if "hide_amounts" not in st.session_state:
    st.session_state["hide_amounts"] = bool(_read_prefs().get("hide_amounts", False))

# Add holdings (the top bar) or a page's own button was pressed (_open_holdings_dialog)
_open = st.session_state.pop("open_dialog", None)
if PAGE in ("Clients", "Admin", "Account", "About") and not _open:
    # these pages are about the login, its clients or the app - not the viewed
    # account's holdings, so they aren't read (a Holdings window needs them)
    snapshot, positions, cash_by_account, quotes, watch_tickers = None, [], {}, {}, []
    SNAPSHOT_SOURCE = None
else:
    _data_conn = connect(DB)   # one connection for the holdings, their source and the watchlist
    try:
        snapshot, positions, cash_by_account, quotes, watch_tickers = load(_data_conn)
        # an import, a hand entry, a percentages portfolio or the example portfolio
        SNAPSHOT_SOURCE = (_LATEST_SOURCE if snapshot == _LATEST_SNAPSHOT   # read above
                           else snapshot_source(_data_conn, USER_ID, snapshot))
    finally:
        _data_conn.close()
# what the value chart prices, from the holdings just loaded (no re-reads)
PERF_BASIS = perf.basis_of(snapshot, positions, cash_by_account)
if _open == "manual" and CAN_IMPORT:
    _manual_dialog(positions, cash_by_account, SNAPSHOT_SOURCE)
elif _open == "import" and CAN_IMPORT:
    _import_dialog()
if not positions and PAGE in ("AI Assistant", "Plan", "Get started", "Advisor notes"):
    # Helping brand-new investors plan a first portfolio is a core use of the
    # assistant, and a goal can be set before there's anything invested, so
    # both work before any CSV has been imported.
    _page_header(_label(PAGE), data=False)
    if PAGE == "Plan":
        _render_plan(None, None, None)
    elif PAGE == "Get started":
        _render_get_started(False, None)
    elif PAGE == "Advisor notes":
        if ON_CLIENT:
            _render_meeting_prep(None, None, None, None)
            _render_report_advisor(None)
        if IS_MANAGED_CLIENT:
            _render_reports_client()
            _render_proposals_client(None, None)
        _render_notes()
    else:
        _render_assistant([], {})
    st.stop()
if PAGE == "Clients":
    # about the advisor's clients, not the viewed account's data
    _page_header(_label(PAGE), data=False)
    _render_clients()
    st.stop()
if PAGE == "Admin":
    # about accounts and logins, not the viewed account's data
    _page_header("Admin", data=False)
    _render_admin()
    st.stop()
if PAGE == "Account":
    # the login's own account, whichever account is being viewed
    _page_header("Account", data=False)
    _render_account()
    st.stop()
if PAGE == "About":
    _page_header("About and disclosures", data=False)
    _render_disclosures()
    st.stop()
if not positions and PAGE != "Watchlist":
    # Nothing brought in yet (the watchlist works regardless - Learn's example
    # funds go on it before anything is bought). What shows depends on whose
    # account it is (views/start_home.py):
    if ON_CLIENT or IS_ADVISOR:
        # an advisor: a client's (or their own) statements to bring in
        _page_header(_label(PAGE), data=False)
        _render_bring_in(ACTIVE_NAME if ON_CLIENT else None)
        if ON_CLIENT and PAGE == "Dashboard":
            _render_client_home(preview=True)   # what the client sees on their Home
    elif IS_MANAGED_CLIENT and PAGE == "Dashboard":
        # an advisor's client: their advisor's next step (client mode)
        _page_header(_label(PAGE), data=False)
        _render_client_home()
    elif not CAN_IMPORT:
        # a client whose advisor brings the statements in
        _page_header("Welcome", data=False)
        st.info(f"Welcome, **{ACTIVE_NAME}**. Your advisor, {_advisor_display_name()}, "
                "brings your statements in - your portfolio shows up here once they have.")
        with st.container(horizontal=True):
            st.button(f"Open {_label('Advisor notes')}", key="onboard_notes", type="primary",
                      on_click=_go, args=("Advisor notes",))
            st.button(f"Open {_label('Get started')}", key="onboard_get_started",
                      on_click=_go, args=("Get started",))
    elif PAGE == "Dashboard":
        # someone not investing yet: Home is their route, not an import form
        _page_header(_label(PAGE), data=False)
        _render_start_home()
    else:
        # Activity, Income: one calm line until there's something to show
        _page_header(_label(PAGE), data=False)
        _render_not_yet(PAGE)
    st.stop()

cash = sum(cash_by_account.values())

_held_symbols = {p["symbol"] for p in positions}
# Deep Yahoo history (moving averages, volume, 52-wk, beta, P/E, sector) - for
# this account's holdings and watchlist only, not every ticker anyone holds.
_my_tickers = _held_symbols | set(watch_tickers)
_bars_conn = connect(DB)   # one connection for the history these need
try:
    bar_stats = perf.bar_stats(_bars_conn, _my_tickers)
    sec_info = perf.security_info(_bars_conn, _my_tickers)
    # Holdings with no Yahoo history yet (a first import, or a new position) -
    # filled in below, once per visit, so the charts fill in without a manual sync
    _covered, _missing = perf.holdings_coverage(_bars_conn, USER_ID, PERF_BASIS)
    # the funds' top holdings kept from Yahoo, for Home's Fund overlap card
    # (fund_holdings.py; nothing is fetched here - the window asks Yahoo)
    fund_tops = (fund_holdings.cached(_bars_conn, fund_holdings.funds_in(positions, sec_info))
                 if PAGE == "Dashboard" and INVESTOR_VIEW else {})
    # The dividends each holding paid while held, for its total return (Home
    # and a holding's details): the imported activity history, else estimated
    # from Yahoo's payments. Not for a percentages portfolio (pretend shares).
    DIVIDENDS = (income.received_while_held(
        _bars_conn, USER_ID, _held_symbols, datetime.now().date(),
        skip_sources=(SAMPLE_SOURCE, manual_entry.PCT_SOURCE))
        if PAGE == "Dashboard" and SNAPSHOT_SOURCE != manual_entry.PCT_SOURCE else {})
finally:
    _bars_conn.close()
# What each holding holds - Stocks / Bonds / Cash / Other (asset_classes.py):
# the account's own choice, else Yahoo's fund breakdown, else the broker type.
CLASS_OVERRIDES = {s: c for s, c in (_read_prefs().get(asset_classes.OVERRIDES_PREF) or {}).items()
                   if c in asset_classes.CLASSES}
CLASS_SPLITS = asset_classes.splits_from(positions, sec_info, CLASS_OVERRIDES)
watch_only = [t for t in watch_tickers if t not in _held_symbols]

# One metric context per position (same order as `positions`). Reused everywhere
# below: totals, alerts, the holdings table. port_value / acct_value are filled
# in once the totals are known.
contexts = [{"pos": p, "quote": quotes.get(p["symbol"], {}),
             "stats": bar_stats.get(p["symbol"], {}), "info": sec_info.get(p["symbol"], {}),
             "port_value": None, "acct_value": None,
             "dividends": d}
            for p, d in zip(positions, income.split_by_holding(positions, DIVIDENDS))]

tot_mv = tot_gl = tot_cost = tot_div = 0.0
acct_value = {}
for p, ctx in zip(positions, contexts):
    mv, cost = M.eff_mv(ctx), p["cost_basis"]
    if mv is not None:
        tot_mv += mv
        acct_value[p["account"]] = acct_value.get(p["account"], 0.0) + mv
        if cost is not None:
            tot_gl += mv - cost
            tot_cost += cost
            tot_div += ctx["dividends"] or 0.0   # only where there's a gain to add them to

for acct, csh in cash_by_account.items():
    acct_value[acct] = acct_value.get(acct, 0.0) + (csh or 0.0)

portfolio_value = tot_mv + cash
tot_glp = (tot_gl / tot_cost * 100) if tot_cost else None
# price change plus dividends (None: no dividends known - the price change alone)
tot_return = income.total_return(tot_gl if tot_cost else None, tot_cost, round(tot_div, 2))
for p, ctx in zip(positions, contexts):
    ctx["port_value"] = portfolio_value
    ctx["acct_value"] = acct_value.get(p["account"])

n_live = sum(1 for p in positions if p["live_price"] is not None)
last_live = max((p["live_price_at"] for p in positions if p["live_price_at"]), default=None)

day_change_total = sum(
    v for v in (M.value("day_change_usd", ctx) for ctx in contexts) if v is not None
)

# ---- log this session's portfolio value (once, throttled) ---------- #
# last_open() must run BEFORE log_open() writes this session's own row, or
# "since you last opened" would just be comparing the portfolio to itself.
if "last_open_snapshot" not in st.session_state:
    st.session_state["last_open_snapshot"] = perf.last_open(DB, USER_ID)
if "value_logged" not in st.session_state and positions:   # (none: the watchlist alone)
    # just saved new holdings (_after_import): log their value now, whatever the
    # gap, so the next visit is compared with them
    _rebase = st.session_state.pop("value_rebase", False)
    st.session_state["value_logged"] = perf.log_open(DB, USER_ID, {
        "snapshot_date": snapshot,
        "portfolio_value": portfolio_value,
        "holdings_value": tot_mv,
        "cash": cash,
        "cost_basis": tot_cost,
        "unrealized_gain": tot_gl,
        "unrealized_gain_pct": tot_glp,
        "day_change_usd": day_change_total,
        "n_positions": len(positions),
        "n_priced": n_live,
        "priced_at": last_live,
    }, **({"min_gap_sec": 0} if _rebase else {}))


# Holdings with no Yahoo history yet (a first import, or a new position;
# _missing, read above): fetch it once per visit so the charts fill in without
# a manual sync. Runs before the header so the header's one-shot messages
# survive its rerun.
# ...and holdings Yahoo was never asked to describe (what a fund holds -
# asset_classes.py). quote_type is None until asked, "" if Yahoo had nothing.
_undescribed = sorted({p["symbol"] for p in positions
                       if (sec_info.get(p["symbol"]) or {}).get("quote_type") is None}
                      - set(_missing))
if PAGE == "Dashboard" and (_missing or _undescribed)         and not st.session_state.get("auto_backfilled"):
    st.session_state["auto_backfilled"] = True
    _sync_history(sorted(set(_missing) | set(_undescribed)), quick=True)

_page_header(_label(PAGE))
hide_amounts = st.session_state["hide_amounts"]


def _pick_holdings():
    st.session_state["watchlist_pill"] = None


def _pick_watchlist():
    st.session_state["holdings_pill"] = None


_view("dashboard_page")


_view("watchlist")


_view("ticker_detail")


_view("activity")


_view("income")


if PAGE == "Plan":
    _render_plan(portfolio_value, tot_gl,
                 allocate(positions, cash_by_account, CLASS_SPLITS)["by_asset_class"])

if PAGE == "Get started":
    _render_get_started(True, portfolio_value)

if PAGE == "Advisor notes":
    if ON_CLIENT:
        _render_meeting_prep(portfolio_value,
                             allocate(positions, cash_by_account, CLASS_SPLITS)["by_asset_class"],
                             contexts, cash_by_account)
        _render_report_advisor(portfolio_value)   # figures are masked on screen, not stored
    if IS_MANAGED_CLIENT:
        _render_reports_client()
        _render_proposals_client(
            allocate(positions, cash_by_account, CLASS_SPLITS)["by_asset_class"],
            None if _hidden() else portfolio_value)
    _render_notes()

if PAGE == "AI Assistant":
    _render_assistant(contexts, cash_by_account)
