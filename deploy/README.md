# Deploying this fork

## TrueNAS SCALE

`truenas.yaml` installs nexdeck as a custom app: Apps > Discover Apps > ⋮ >
Install via YAML. Change the `image:` line and the dataset path first.

### The image

The Extensions feature exists only in this fork, so the upstream image
`ghcr.io/derkezorm/nexdeck` does not have it. The CI workflow builds
`ghcr.io/nulljayden/nexdeck` on every push to `main`, once:

1. the branch with the feature is merged into `main`,
2. Actions are switched on for the repository (a fork has them off at first:
   the Actions tab > "I understand my workflows, go ahead and enable them"),
3. the package is reachable from the NAS: GitHub > your profile > Packages >
   nexdeck > Package settings > Change visibility > Public. Or keep it private
   and add a registry credential in TrueNAS (Apps > Configuration > Manage
   Container Images / registry credentials) with a GitHub token that has
   `read:packages`.

Updating later: Apps > nexdeck > Edit, Save (or the update button when TrueNAS
offers one) pulls `latest` again.
