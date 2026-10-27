# What to blur on a shared screen

A reel of a talk exists so viewers can see how a tool works. Blur the private
**words** on the screen, never the screen itself. (Kyle, on the Talk #5 draft:
"whenever you show a screen, you basically blocked out the entire screen. It's
kind of pointless." And: "Just be reasonable about what you take out.")

## The rule

Blur a word when a stranger watching the reel could learn something private
from it. Keep everything else readable.

| Hide (blur the word or the line) | Keep (leave it readable) |
|---|---|
| Real people's names other than the presenters: staff, family, members, contacts | The presenters' own names, and the names of the presenters' bots ("Vince Vega", "Dr. Eggbot", "Jules") |
| Customer, client and company names; private project or deal names | A bot's avatar and the way it is personalized |
| Money: revenue, prices, balances, invoice totals ("$12,400", "4.5M USD") | Generic task and chat text ("Submit marketing by Friday", "Q3 roadmap", "10 tasks") |
| Personal details: a child's name, school or age, a personal e-mail inbox, health details, a home address, a town | Product and site names ("Asana", "Claude", "asana.com", a public repo path) |
| E-mail addresses, phone numbers, keys and tokens, card numbers | Menus, buttons, headings, labels, dates |
| Links and IDs that point at one record ("app.asana.com/0/1203344556/list", "...?id=77") | The layout of the app: panes, columns, the shape of a workflow |

When unsure, ask whether the word identifies a person, a customer or an amount.
If it does not, keep it.

## How the bot applies it

- `--redact` with no value is `--redact auto`: OCR reads the frames the reel shows and
  blurs only the matching words. Built in: e-mail addresses, phone numbers, keys and
  tokens, card numbers, SSNs, money amounts and links that carry an ID.
- Names, clients, projects and places can't be told from ordinary words by a pattern.
  List them in a terms file (`--redact-terms names.txt`: one per line, `re:` for a
  regular expression, `#` for comments) and include the OCR misreadings the leak check
  turns up. Talk #3's file (people, companies, medical words and their OCR variants)
  is the model to copy.
- A hand-drawn `redactions.json` is for the few spots OCR can't read, like an image
  of text, and its boxes should be about the size of a word or a line. The run
  **refuses** a file whose boxes cover more than 40% of the frame at once, and names
  the time ranges. `--allow-large-blur` overrides this, but only for a screen that
  is private from edge to edge, such as someone's personal inbox shown by accident.
- `--redact text` blurs every line of text and only warns; it has the same problem
  and is rarely the right choice for a reel.

## Check by eye

`clipbot check-redaction` reads the rendered reel back for anything still readable.
After that, look at each distinct screen at full size or zoomed in. Every sensitive
word should be gone, and the screen should still be readable. A screen that is a grey
smear fails the check, just as a readable client name does.
