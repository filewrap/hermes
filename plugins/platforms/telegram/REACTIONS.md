# Telegram message reactions

The active MTProto adapter can react to incoming user messages using local,
deterministic text rules. Reaction selection never calls a model or creates an
extra agent turn. This is an independent heuristic, not Cue's private algorithm.
Normal message processing is unchanged.

## Control your reactions

In a private bot chat, open `/settings` and use **Message reactions** to switch
reactions off or back on. They are enabled by default, including for existing
settings files without the new `reactions_enabled` field. The choice is saved
for your Telegram user in the routed profile; it does not change another user's
settings. Your choice applies to admitted messages in both DMs and groups.

## How selection works

`intent_reactions.py` contains the ordered rules and their rationale. For
example, a suggestion such as “I suggest adding reactions” gets 👀, while an
unambiguous success report such as “it worked” gets 🎉. Negative or sensitive
messages take precedence over positive keywords. The selector can use an
already-fetched reply to the bot as bounded context; it never fetches history
or sends message text anywhere to decide an emoji.

Ambiguous or unmatched messages can receive no reaction. Slash commands,
forwarded messages, bot senders and channel posts are skipped. Authorization and
group-trigger checks run before reaction selection, and private settings input
is consumed before this feature sees it. Processing success or failure does not
replace the intent reaction with a status emoji.

Telegram may restrict reactions or reject an emoji. Delivery is best-effort,
with a short timeout and no retry, fallback message, or disruption of the normal
response. Heuristics cannot guarantee correct interpretation of sarcasm,
negation, every language, or complex mixed-intent messages.

## Loading a code change

The running gateway is not hot-reloaded by editing or pushing these files. A
normal, separately authorized gateway restart is needed to load the change.
