Fake game for the game_research end-to-end tests: no real engine, small text files only.
- source/: the project as its source tree (engine "fixture", detected by game.json).
- build/: the same game shipped as a build only (build_info.json + game.pck), no source.
- captures/: playtest observations transcribed as level maps ("?" = not seen). level1_a and
  level1_b overlap and disagree on one cell; level2 is never captured.
