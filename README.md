# DevGuides

The hub of https://dev-guides.home/: the estate's guides for agents, one site per guide under its
own path, with an index page and a root `llms.txt` over them. This repo holds the hub, the checker
every guide is held to, and the one Jenkins pipeline that builds and publishes them all. The
guides themselves live in the source repos on `sources.txt`, this one included.

The guide contract (what a guide folder holds, its `site_url`, its nav, its `llms.txt`, its rule
anchors) is the `dev-guides` skill in the kubecoder plugin. This README describes the repo, not
the contract.

## What is here

- **`tools/check_guide.py <repo-dir>`** builds every guide in a source repo with the guide's own
  toolchain and checks it against the contract. It is every source repo's `kc project test`.
- **`tools/build_hub.py [<root>]`** builds the site into `out/`: it runs the checker over every
  repo on `sources.txt`, copies each guide's `site/` to `out/<path>/`, and writes the index page
  and the root `llms.txt`. It is the one definition of the site's layout; the `Dockerfile` and this
  repo's `kc project test` both run it.
- **`sources.txt`** names the source repos, side by side under one directory: the Jenkins
  workspace after `Checkout`, or `/work` in a pod.
- **`landing/`** is the index page's template and its favicon. `build_hub.py` writes one card per
  guide into it, from the guide's `site_name` and `site_description`.
- **`tools/stage_context.py`** copies the source repos' working trees, without what git ignores,
  into the image's build context for `kc project build`.
- **`Dockerfile`** and **`nginx.conf`** make the `registry:5000/dev-guides-home` image: a Python
  stage that runs `build_hub.py`, then nginx serving `out/`.
- **`Jenkinsfile`** is the job `DevGuides`.

Public guides can live here too, as top-level folders like any source repo's.

## Building it

- **`kc project test`** builds the whole hub over the source repos' working trees under `/work`:
  every guide built and checked, then the index and the root `llms.txt` assembled and their links
  checked. It is green only when every source repo's guides are.
- **`kc project build`** stages the context under `/work/scratch/dev-guides-context` and builds the
  image with `kaniko --no-push`, as the job builds it.

## Publishing it

The job `DevGuides` clones this repo and every other repo on `sources.txt` in `Checkout`, builds
the image (the checks run inside the image build, so a failing guide never ships), and pins it
into `pvginkel/DevGuidesDeploy` at `images.devGuides`. Argo CD deploys it from there.

The clones in `Checkout` are also the triggers: a push to any repo the job's last build checked out
starts the job, so the source repos have no job of their own. One guide's failure blocks the
publish of all of them.

## Checking a guide in another repo

Every other source repo runs this repo's checker from its raw URL, so there is one copy:

```
uv run https://raw.githubusercontent.com/pvginkel/DevGuides/main/tools/check_guide.py <repo-dir>
```

A change to the checker changes every repo's gate once it is on `main`. Before then, run it from
this tree: `uv run /work/DevGuides/tools/check_guide.py <repo-dir>`.

## Adding a source repo

A new guide in an existing source repo needs nothing here. A new source repo takes two edits in
the same commit:

1. its name in `sources.txt`;
2. its clone in the `Jenkinsfile`'s `Checkout` stage, a `dir('<Repo>') { git … }` beside the
   others.

`build_hub.py` fails when a repo on `sources.txt` was not cloned.
