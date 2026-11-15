# What is deliberately not done yet

A thing left undone with no trigger is a thing forgotten. Each row names what
would make it worth doing, so the question comes back on its own rather than
depending on someone remembering it. Nothing enforces a trigger; the list is
read whenever a decision record is written and when a phase ends.

| Not done | The trigger | Why not now |
|---|---|---|
| A coverage threshold in CI | Two months of measured coverage to set it below | A number picked before there is data is a guess with a gate on it |
| A code of conduct and issue templates | A second contributor, or the first outside issue | Boilerplate answering questions nobody has asked |
| A release workflow and publishing | The first release someone outside will install | A published name is claimed and a published version cannot be reused |
| `CODEOWNERS` | A second person who reviews | It would name one person as the owner of everything |
| `pre-commit` as a requirement rather than an option | A second contributor | The gates run in CI, and a hook one person installs is a hook one person maintains |
| Any command: start, stop, pause, resume, a current limit | The owner asks for it by name, after a day of read-only use | Each needs a spoken yes, a read-back and its own review; ADR-EM-001 |
| Keeping the refresh token on disk | The command phase, or logins found to cost too much | It would be a second written secret; ADR-EM-002 |
| The SignalR stream for live state | Polling found too slow or too costly | Its open callback sends a subscription; a reviewed change of its own |
| Choosing between several chargers by name | A second charger on the account | The account has one; the first found is used |
| Reasons for no current, in words | The owner asks why a car is not charging | pyeasee's table would be copied; only the number is read today |
| Mutation rows for the mutation script's own four rules | garage-door proves `scripts/mutate.py` can safely switch off its own rules, and template v8 carries it | The script that runs the mutations would be the file mutated; a fix copied before it is proven could leave a rule switched off |
