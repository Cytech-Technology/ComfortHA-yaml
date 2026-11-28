# Home Assistant App: Cytech AI Assistant

Ask questions about your Cytech Comfort alarm and home-automation system in plain
language, from a panel inside Home Assistant.

The assistant reads the current state of this Home Assistant — entities and logs —
and explains what it finds. It is **read-only**: it cannot arm or disarm the alarm,
bypass zones, switch outputs, run scripts, or change anything.

**Target architectures** are `aarch64` and `amd64`.

For more information about Cytech Comfort systems, please see the
[Cytech Technology Pte Ltd.](https://cytech.biz) website.

## About

This App answers owner and installer questions such as:

- "Is my alarm in trouble?" — checks the raw Comfort status, not just the
  alarm panel entity that shows `disarmed` for Trouble and Alert.
- "Which zones are open?"
- "How is my system health?" — battery, SD card, restarts, network.

It uses Cytech's AI service. Each installation is issued a token, and usage is
limited to a monthly allowance; contact Cytech if you need more.

## Requirements

- Home Assistant OS with the Cytech Comfort App installed (recommended — the
  assistant explains the entities that App creates).
- An installation token from Cytech or your installer.
- Internet access from the Home Assistant device.

## Installation

1. In Home Assistant, go to **Settings → Apps**, then **Install App**.
2. Click the **⋮ (three dots)** in the top-right corner → **Repositories**.
3. Add `https://github.com/Cytech-Technology/ComfortHA-yaml`.
4. Install **Cytech AI Assistant**.
5. Open its **Configuration** tab and paste the installation token supplied by
   Cytech or your installer. Leave the other options as they are.
6. Start the App and open **AI Assistant** in the sidebar.

## Privacy

Conversations are processed by Anthropic's Claude API through Cytech's
`ai.cytech.biz` service. Cytech's server stores only a hash of the installation
token and per-month token counts — never chat content. The App talks to Home
Assistant with read-only GET requests. Never type passwords, alarm codes,
remote-access links, or `cytech_secrets` content into the chat.

See [DOCS.md](DOCS.md) for full documentation.

Copyright 2026 Cytech Technology Pte Ltd. Licensed under Apache-2.0. For more
details see the [LICENSE](LICENSE) file.
