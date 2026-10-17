"""Design tokens and CSS. Only our own `tp-` classes are styled, plus a few stable Streamlit hooks."""

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap');

:root {
  --tp-bg: #FAF6EF;
  --tp-surface: #FFFFFF;
  --tp-ink: #1E2A22;
  --tp-muted: #5B685F;
  --tp-line: #E6DECF;
  --tp-brand: #2F5D3A;
  --tp-brand-soft: #E8F0E6;
  --tp-saffron: #D98E04;
  --tp-saffron-soft: #FFF1D6;
  --tp-saffron-ink: #6F4300;
  --tp-danger: #9B2C2C;
  --tp-danger-soft: #FCEBEA;
  --tp-radius: 18px;
  --tp-shadow: 0 1px 2px rgba(30, 42, 34, .06), 0 6px 18px rgba(30, 42, 34, .05);
}

.stApp { direction: rtl; font-family: 'IBM Plex Sans Arabic', 'Segoe UI', Tahoma, sans-serif; }
.block-container { max-width: 820px; padding: 1.2rem 1rem 7rem; }
.stApp button { font-family: inherit; }
.stButton > button { border-radius: 999px; border: 1px solid var(--tp-line); background: var(--tp-surface);
  color: var(--tp-ink); font-weight: 500; transition: border-color .15s, background .15s; }
.stButton > button:hover { border-color: var(--tp-brand); background: var(--tp-brand-soft); color: var(--tp-ink); }
[data-testid="stChatInput"] textarea { direction: rtl; text-align: right; font-family: inherit; }

.tp-root { direction: rtl; text-align: right; color: var(--tp-ink); line-height: 1.75; }

/* brand */
.tp-brand { display: flex; align-items: center; gap: .75rem; padding: .25rem 0 .5rem; }
.tp-logo { width: 44px; height: 44px; flex: none; }
.tp-brand-name { font-weight: 700; font-size: 1.25rem; letter-spacing: .2px; direction: ltr; text-align: right; }
.tp-brand-tag { color: var(--tp-muted); font-size: .9rem; }

/* hero / empty state */
.tp-hero { background: var(--tp-surface); border: 1px solid var(--tp-line); border-radius: 24px;
  padding: 1.6rem 1.4rem; margin: .6rem 0 1rem; box-shadow: var(--tp-shadow); }
.tp-hero-title { font-size: 1.7rem; font-weight: 700; line-height: 1.4; margin: 0 0 .4rem; }
.tp-hero-lead { color: var(--tp-muted); margin: 0 0 1.1rem; font-size: 1.02rem; }
.tp-points { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .75rem; }
.tp-point { background: var(--tp-bg); border-radius: 14px; padding: .8rem .9rem; }
.tp-point-title { font-weight: 600; margin-bottom: .1rem; }
.tp-point-text { color: var(--tp-muted); font-size: .9rem; line-height: 1.6; }
.tp-suggest-label { color: var(--tp-muted); font-size: .9rem; margin: .4rem 0 .3rem; }

/* preferences strip */
.tp-prefs { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; margin: .2rem 0 .8rem; }
.tp-prefs-label { color: var(--tp-muted); font-size: .85rem; margin-inline-end: .2rem; }
.tp-chip { display: inline-block; padding: .1rem .65rem; border-radius: 999px; font-size: .85rem;
  background: var(--tp-brand-soft); color: var(--tp-brand); font-weight: 500; }
.tp-chip-warn { background: var(--tp-saffron-soft); color: var(--tp-saffron-ink); }
.tp-chip-muted { background: #EFEAE0; color: var(--tp-muted); }

/* messages */
.tp-msg { display: flex; margin: .55rem 0; gap: .55rem; align-items: flex-start; }
.tp-msg.tp-user { justify-content: flex-start; }
.tp-avatar { width: 30px; height: 30px; flex: none; margin-top: .15rem; }
.tp-bubble { max-width: min(88%, 640px); padding: .7rem 1rem; border-radius: var(--tp-radius);
  overflow-wrap: anywhere; box-shadow: var(--tp-shadow); }
.tp-user .tp-bubble { background: var(--tp-brand); color: #fff; border-end-start-radius: 6px; }
.tp-assistant .tp-bubble { background: var(--tp-surface); border: 1px solid var(--tp-line); border-start-start-radius: 6px; }
.tp-ask .tp-bubble { border-color: #CFE0CB; background: #F6FAF4; }
.tp-error .tp-bubble { border-color: #E9C4C2; background: var(--tp-danger-soft); color: var(--tp-danger); }

/* no results */
.tp-callout { background: var(--tp-saffron-soft); border: 1px solid #F0D79A; color: var(--tp-saffron-ink);
  border-radius: var(--tp-radius); padding: .9rem 1rem; display: flex; gap: .7rem; align-items: flex-start; }
.tp-callout-icon { font-size: 1.3rem; line-height: 1.4; }

/* dish cards */
.tp-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: .8rem; margin: .5rem 0 .4rem; }
.tp-card { background: var(--tp-surface); border: 1px solid var(--tp-line); border-radius: var(--tp-radius);
  padding: 1rem; box-shadow: var(--tp-shadow); display: flex; flex-direction: column; gap: .5rem; }
.tp-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: .6rem; }
.tp-card-alt { border-style: dashed; background: #FDFBF6; }
.tp-card-title { font-weight: 700; font-size: 1.1rem; line-height: 1.4; margin-top: .25rem; }
.tp-cat { font-size: .78rem; color: var(--tp-muted); }
.tp-price { flex: none; background: var(--tp-saffron-soft); color: var(--tp-saffron-ink); font-weight: 700;
  border-radius: 12px; padding: .15rem .6rem; font-size: .95rem; white-space: nowrap; }
.tp-desc { color: var(--tp-muted); font-size: .92rem; margin: 0; }
.tp-reason { border-inline-start: 3px solid var(--tp-brand); padding-inline-start: .6rem; font-size: .92rem; }
.tp-facts { display: grid; gap: .25rem; font-size: .85rem; margin-top: auto; }
.tp-fact { display: grid; grid-template-columns: 6.4rem 1fr; gap: .4rem; align-items: baseline; }
.tp-fact-label { color: var(--tp-muted); }
.tp-spice { display: inline-flex; gap: 3px; vertical-align: middle; margin-inline-end: .35rem; }
.tp-spice i { width: 8px; height: 8px; border-radius: 50%; background: #E3DACA; display: inline-block; }
.tp-spice i.on { background: #C4491C; }
.tp-lead { margin-bottom: .1rem; }

/* notices */
.tp-notice { font-size: .85rem; color: var(--tp-muted); background: #F4EFE5; border-radius: 12px;
  padding: .55rem .8rem; margin: .4rem 0; }
.tp-notice.tp-notice-warn { background: var(--tp-danger-soft); color: var(--tp-danger); }
.tp-foot { text-align: center; color: var(--tp-muted); font-size: .78rem; margin-top: 1.2rem; }

/* typing indicator */
.tp-typing { display: inline-flex; gap: 5px; padding: .35rem .1rem; }
.tp-typing i { width: 7px; height: 7px; border-radius: 50%; background: var(--tp-brand); opacity: .35;
  animation: tp-blink 1.2s infinite ease-in-out; }
.tp-typing i:nth-child(2) { animation-delay: .2s; }
.tp-typing i:nth-child(3) { animation-delay: .4s; }
@keyframes tp-blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .tp-typing i { animation: none; opacity: .6; } }

@media (max-width: 520px) {
  .tp-hero { padding: 1.2rem 1rem; }
  .tp-hero-title { font-size: 1.4rem; }
  .tp-bubble { max-width: 94%; }
}
</style>
"""
