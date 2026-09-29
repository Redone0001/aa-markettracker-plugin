# Repository workflow

- For each completed change set, increment the patch component of the package version (x.y.z to x.y.(z+1)). Preserve any prerelease suffix unless the user requests a stable release. Update version assertions as needed.
- Run relevant checks, commit the completed changes, and push to the user repository by default unless the user explicitly asks otherwise.
- Do not include unrelated user changes in a commit or use force pushes.
