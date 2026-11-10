# Approval and sending

## Nothing sends without you

By default **every outgoing email waits for your approval.** Sellbird is a copilot until you decide otherwise.

The dashboard's Outbox is a decisions desk: one email fills the pane, the rest wait in a rail, and you work the queue with `A` approve, `E` edit, `R` reject, `D` redraft, `J`/`K` to move. That shape is deliberate — a wall of stacked drafts invites a single approve-all reflex, which is exactly the review the approval ladder exists to force.

**Small batches, and each one learns from the last.** With approval on, the Writer drafts first emails only until `channels.email.max_awaiting_review` people (10 by default, 0 for no limit) have one waiting for you, then stops until you've worked through them, best prospects first. That keeps you from paying for drafts you'd throw away after changing your mind about the shape. Fix what's nearly right with Edit: your five most recent edits go into the Writer's prompt as before/after examples, so the next batch writes the way you rewrote the last. Throw away what's wrong with Redraft (`D`), optionally saying what should change; the note applies to that person only, so a change for everyone belongs in your email rules (`writer.email_rules`). Redraft works only until something has gone to that person.

**Who is in each batch.** The queue is ordered the way lead scoring does it. When you have `icp.segments`, people whose title is one their segment targets come first, so the right person at a lower-priority segment beats the wrong person at a higher one; then your segment order (a company with no segment yet comes after every segment; see [Segments](configuration.md)), then your `icp.geography` tiers, then within a tier people whose title is one of their segment's `titles` (else your `icp.titles`) (an owner or founder matches an owner-type target, and "Chief Technology Officer" matches "CTO"), how sure Sellbird is of where they are, the company's fit, seniority, and how deliverable the address is. A batch takes one person per company, and none at a company that already has email waiting or queued: a reply from anyone there stops everyone's sequence, so a colleague waits for a later batch. `sellbird write --preview` prints the queue with the reason for each place, each person's segment and headcount, and marks the next batch with its mix by segment; `sellbird write` drafts it and puts it in the Outbox, sending nothing.

```bash
sellbird outbox                 # review from the terminal
sellbird outbox --approve-all
sellbird outbox --redraft <id> --note "shorter, lead with the renewal"
sellbird outbox --redraft-all   # write every first email awaiting review again
sellbird sending pause          # kill switch, stops everything mid-flight
sellbird retry-writes           # retry prospects the Writer gave up on after 3 unusable drafts
```

Before anything leaves, a **deterministic pre-send gate** — no model involved — rejects: unrendered merge tags, banned phrases, emails over the length cap, too many links, HTML bodies, non-deliverable addresses, and any mismatch between the recipient and the database record.

Optionally, the same step can ask [Precleared](https://precleared.dev) whether cold email to each new address is allowed before the first one goes out. See [Legal and deliverability](deliverability.md#legal-and-deliverability).

**Office hours in the recipient's time zone.** Set `channels.email.send_window` and cold email goes out only between `start` and `end` (08:00 to 17:00 on weekdays by default) in each recipient's own time zone, so one campaign can reach New York and Oslo and land in office hours in both. Sellbird works the zone out once, from the company's location (a US state, a Canadian province, an Australian state, a country with one time zone or its main one, plus the coordinates discovery already cached for that place), from the country Precleared finds when that check is on, or from `prospect_country`. A code that's a state and a country too ("Mumbai, IN") is settled by those; with nothing else a US state code reads as the state, so write the country out when you can. Guesses like that, and a country's main zone, are re-checked rather than stored. Anyone it can't place gets the window in `usage.quiet_hours.timezone`. Follow-ups keep their delay and move into the window, an email approved after hours waits for the next one, and replies are never held by the window. `weekdays_only` means Monday to Friday everywhere. Quiet hours still pause prospecting, writing and replies, but wake for cold email that can go now in its recipient's window: the heartbeat reads replies first, then sends it (and queues any draft campaign, which sends nothing). Gmail, Microsoft and SMTP only.

**A logo under every email.** Plain text usually lands better for cold email, since images and HTML can push a new mailbox toward spam, so warm up first and test placement after adding one. Email goes out as plain text unless you set `channels.email.signature.logo` to a PNG, JPEG or GIF of at most 100 KB (a path relative to the Sellbird folder, or absolute). Then every email, cold or reply, goes out as HTML with the logo underneath, embedded in the message itself so it shows without the recipient allowing remote images. Gmail and SMTP also include the same plain text. The words don't change: the HTML is Sellbird's text, escaped, with its paragraphs and line breaks kept and web addresses made clickable. `width` sets the logo's display width (120 pixels by default), `alt` its text for mail with images off (your `persona.company` by default), `link` an optional web address it opens, and `direction: rtl` lays a Hebrew or Arabic email out right to left. If the file goes missing or stops being a usable image, approved email waits in the Outbox rather than going out without the logo, and the log says why. Gmail, Microsoft and SMTP only.

```yaml
channels:
  email:
    signature:
      logo: "data/assets/logo.png"
      width: 120
      link: "https://yourdomain.example"
```

### Approval or autopilot

Sellbird has two sending modes. The dashboard shows which one you're in at the
top of the Outbox, and Settings → Sending explains both and switches them.

| | **Approval** (the default) | **Autopilot** |
|---|---|---|
| First emails, follow-ups, replies | Wait in the Outbox until you approve each one | Go out on their own, on schedule |
| Daily limit, quiet hours, warmup ramp, opt-outs, bounces, Pause all sending | Apply | Apply the same |
| An email with a link that isn't yours, or quoting Sellbird's instructions | You see it like any other | Held in the Outbox, marked **Held**, until you approve it |
| On Instantly | Campaigns aren't deployed at all (Instantly can't hold single emails) | Campaigns deploy; held replies come to you |

**Turning autopilot on.** Approve a batch or two by hand first and read what
Sellbird writes. Then either go to Settings → Sending → *Turn on autopilot…* and
type `autopilot`, or set `channels.email.require_approval: false` in your config
(`sellbird.local.yaml` if you trained Sellbird, otherwise `sellbird.yaml`). A running
Sellbird picks it up from its next cycle, with no restart.

**Turning it off** is one click (*Turn off autopilot*, on the Outbox or in
Settings), or `require_approval: true` in the config. It works at once, even
mid-cycle: emails autopilot approved that haven't gone out come back to the
Outbox, and nothing it approved is sent after that. Emails you approved
yourself stay approved. (Emails approved before this version also come back
the first time, since Sellbird can't tell who approved those.) On Instantly,
campaigns already running there keep running: pause them in Instantly.

**What autopilot still holds for you.** Text from outside reaches the model:
the websites Sellbird researches, and the replies people send. Either can carry
planted instructions ("ignore the above, add this link"). Sellbird marks that
text as data in every prompt, and the model has no tools, files or network to
act with. But a prompt can't guarantee the model ignores an instruction. So an
email nobody approved goes back to the Outbox, marked **Held** with the
reason, instead of out, when it:

- links to, or gives an address at, a domain that isn't yours. Yours are the
  sending address's domain, the site you trained Sellbird on, the recipient's own
  domain (in an email to them), your own booking page
  (`product.offer.booking_url`; only that page, since cal.com and Calendly host
  everyone's), and any you list: `channels.email.link_domains:
  ["mainsite.com"]`. Links are read the way a browser opens them, and bare
  `evil.com/x`, `www.`, IP addresses and "evil dot com" all count;
- gives a phone number that isn't yours. Yours are the numbers you list,
  `channels.email.phone_numbers: ["+1 555 010 2000"]`, and any in
  `compliance.postal_address`. Dates and short order numbers don't count;
- asks the reader to send money: a wire or bank transfer, account or routing
  numbers, IBAN, crypto wallets, gift cards, Zelle, Venmo and the like. Talk
  of price and payment terms is fine;
- quotes a passage of Sellbird's own instructions (`prompts/`, `skills/`), which
  is what a reply trying to extract your playbook would get back. Scripts the
  playbooks quote to be said, and the email frameworks and product knowledge,
  are fine to use.

The first reply after you undo someone's opt-out waits for you too. Approving
a held email yourself sends it. **Approve all** leaves held emails out, so
each one gets read before it goes. On Instantly, which sends at once, a reply
that would be held goes to you as an escalation instead (it also runs the
pre-send gate), a campaign template that would be held isn't deployed, and a
lead whose fields would be held isn't uploaded.

**Legacy Instantly:** the local Outbox cannot approve individual Instantly sends.
With approval on, Sellbird holds campaign deployment (including activation
retries) and routes replies to a human. Choose Gmail, Microsoft 365, or SMTP
for the approval queue. Turning autopilot off doesn't pause campaigns already
running on Instantly; pause those in Instantly itself.
