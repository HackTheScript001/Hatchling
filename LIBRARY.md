# Hatchling feature library

This repository holds the shared feature library that Hatchling's **Feature library** page reads.

- `library/index.json` - the list Hatchling downloads
- `library/features/<make>/` - one file per feature
- `library/confirmations.json` - "works on my car" counts (created on the first one)
- `tools/library_tool.py` - checks and adds features (run by the workflow, never by hand unless you want to)
- `.github/` - the "Share a feature" and "Works on my car" issue forms, and the workflow that processes them

## How features get in

1. Someone presses **Share to library** in Hatchling. GitHub opens a "Share a feature" issue with the feature filled in.
2. You read it. If it looks right, add the label **approved**.
3. The *Feature library* workflow checks it (no VINs, sane values, known fields only), adds it to `library/`,
   rebuilds the index, comments and closes the issue. If it fails the checks it says why and removes the label.

Only people with write access can add labels, so only maintainers can approve.

## "Works on my car"

When someone marks a library feature as working, Hatchling offers to open a "Works on my car" issue. The workflow
counts one per GitHub user, grouped by module part number, and closes the issue.

## Rules for contributors

- Only share features read from your own car.
- Never include a VIN or personal details (the checks refuse them).
- Nothing copied from other tools' databases - only what you learned on your own car.
