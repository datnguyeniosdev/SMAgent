"""Game Intelligence: the domain layer behind the `game_research` plugin (docs/GAME_INTELLIGENCE_AUDIT.md).
Not a plugin of its own: it uses the gateway's Store, ArtifactStore and workflow engine."""

# Its runs are kept out of retention by Settings.retention_exempt_domains (default: game_research).
