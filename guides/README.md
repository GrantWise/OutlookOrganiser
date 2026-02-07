# Getting Started with Outlook AI Assistant

These guides walk you through setting up and using the Outlook AI Assistant from scratch. No prior experience with Azure, Microsoft Entra ID, or OAuth is required.

## Prerequisites

Before you begin, make sure you have:

- A **Microsoft 365 account** (work, school, or personal Outlook.com) with email you want to organize
- An **Anthropic API key** for Claude -- sign up at [console.anthropic.com](https://console.anthropic.com/) (classification costs roughly $0.10-0.30/day for a typical mailbox)
- **Docker Desktop** installed and running ([get Docker](https://www.docker.com/get-docker)) -- or Python 3.12+ if you prefer a local install
- A **web browser** for the Azure Portal setup and device code authentication
- A **terminal** (Command Prompt, PowerShell, Terminal.app, or any Linux terminal) where you can paste commands
- About **20 minutes** for the full initial setup

## Guides

Work through these in order. Each one builds on the previous step.

| # | Guide | Time | What You Will Do |
|---|-------|------|------------------|
| 1 | [Azure AD Setup](01-azure-setup.md) | ~10 min | Register an app with Microsoft so the assistant can read your email |
| 2 | [Installation](02-installation.md) | ~5 min | Install the software and enter your credentials |
| 3 | [First Run](03-first-run.md) | ~10 min | Scan your mailbox, review the proposed organization, and go live |
| 4 | [Daily Usage](04-daily-usage.md) | Reference | Learn the web UI and daily review workflow |
| 5 | [Troubleshooting](05-troubleshooting.md) | As needed | Fix common problems |

## What These Guides Do NOT Cover

These guides focus on getting you up and running. For deeper topics, see:

- **Architecture and code conventions** -- [CLAUDE.md](../CLAUDE.md)
- **CLI command reference** -- [README.md](../README.md#cli-commands)
- **Full configuration options** -- [config.yaml.example](../config/config.yaml.example)
- **Token lifecycle and MSAL internals** -- [Reference/spec/07-setup-guide.md](../Reference/spec/07-setup-guide.md)
- **System behavior specs** -- [Reference/spec/03-agent-behaviors.md](../Reference/spec/03-agent-behaviors.md)
