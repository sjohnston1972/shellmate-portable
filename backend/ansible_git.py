"""
ansible_git.py — Keeping a playbook's history somewhere that has one (#609).

Saving a playbook puts it in ShellMate's library and, from there, on the
runner. Neither is version control, and a file that changes a hundred
devices deserves to be able to answer "what did this look like last
Tuesday, and who changed it".

So a playbook can also be committed to GitHub each time it is saved. Five
things decided rather than assumed, each of them a way this could go
quietly wrong:

**Whose token.** Its own setting, in the vault like every other credential,
and read from nowhere else. Deliberately no `.env` fallback: `GITHUB_TOKEN`
is a name that already exists in a great many development environments —
including the one ShellMate itself is built in — and picking it up would
commit a user's estate under a developer's identity, with nothing on screen
saying so.

**Two ways in, because they need different tokens.** Creating a repository
needs a token that can create repositories, which is a much larger
permission than pushing to one that exists. Somebody who only wants the
second should be able to hand over only the second, so "use this existing
one" is a first-class choice rather than what you do after the create fails.

**Private unless said otherwise.** A playbook carries hostnames, addresses
and the shape of an estate. A public repository created by accident cannot
be un-published — the mirrors have it — so the default is private and
`visibility` has to be spelled out to be anything else.

**The playbook, and nothing else.** Never the inventory. It is generated
from the estate, it is the whole device list, and it would then be sitting
in a repository whose visibility somebody can change later.

**A failure here never costs the save.** The caller writes to the library
first and calls this afterwards; everything here reports rather than
raises upward through the save. Losing somebody's work because GitHub was
unreachable would be an appalling trade for a feature about not losing
work.
"""

import base64
import logging

import httpx

from backend import settings_store

logger = logging.getLogger(__name__)

#: GitHub's API. Not configurable: an "API base" field is how a credential
#: ends up posted to a host nobody meant to trust, and GitHub Enterprise
#: is a different enough proposition to be its own decision.
API = "https://api.github.com"

#: Bounded, because a save must not hang on it. Short enough that a
#: hanging network reads as "not committed" within a few seconds and the
#: playbook is already safe on disk by then.
TIMEOUT = 15.0

#: Where a playbook goes inside the repository. A repository holding
#: nothing but playbooks still benefits from the directory: it leaves room
#: for the README, and it matches what `ansible-runner` expects to find.
FOLDER = "playbooks"


class GitError(Exception):
    """Something GitHub refused, or could not be reached to ask."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        #: A short machine-readable reason, so the interface can say
        #: something different for "no token" than for "no network".
        self.code = code


def token() -> str:
    """
    The token, from the vault and nowhere else.

    No environment fallback, deliberately — see the module docstring. A
    blank answer is the honest one when the vault is locked; the caller
    reports "not configured" and the save stands, which is the whole
    point of a locked vault degrading to "no value" rather than raising.
    """
    try:
        from backend.vault import vault

        return (vault.get("ansible_github_token", "") or "").strip()
    except Exception:                                     # pragma: no cover
        return ""


def config() -> dict:
    """
    What is configured, without the token itself.

    `has_token` rather than the value: this answer reaches the browser, and
    a settings screen only needs to know whether one is there.
    """
    try:
        ansible = settings_store.peek("ansible") or {}
    except Exception:                                     # pragma: no cover
        ansible = {}
    return {
        "has_token": bool(token()),
        "enabled": bool(ansible.get("github_enabled", False)),
        "owner": str(ansible.get("github_owner") or "").strip(),
        "repo": str(ansible.get("github_repo") or "").strip(),
        "visibility": "public" if ansible.get("github_public") else "private",
    }


def _headers(auth: str) -> dict:
    return {
        "Authorization": f"Bearer {auth}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ShellMate",
    }


def _call(method: str, path: str, auth: str, **kwargs) -> httpx.Response:
    """
    One request, with every transport failure turned into a GitError.

    The distinction the codes carry is the one that decides what somebody
    does next: `unreachable` is a network to check, `auth` is a token to
    replace, `refused` is a permission or a name to look at. Reporting the
    first as the second sends people to regenerate a token that was fine.
    """
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, f"{API}{path}",
                                      headers=_headers(auth), **kwargs)
    except httpx.HTTPError as exc:
        raise GitError(f"GitHub could not be reached: {exc}", "unreachable") from exc

    if response.status_code == 401:
        raise GitError("GitHub rejected the token. It may have expired, or "
                       "been revoked.", "auth")
    if response.status_code == 403:
        raise GitError("GitHub refused: the token does not carry the "
                       "permission this needs, or the rate limit is spent.",
                       "forbidden")
    return response


def check() -> dict:
    """Who the token belongs to, so a settings screen can say so."""
    auth = token()
    if not auth:
        raise GitError("No GitHub token is saved.", "no-token")
    response = _call("GET", "/user", auth)
    if response.status_code != 200:
        raise GitError(_why(response), "refused")
    body = response.json()
    # The scopes header is advisory — a fine-grained token sends none —
    # so it is reported rather than acted on. Refusing to try because a
    # header was absent would block the token type GitHub now recommends.
    scopes = response.headers.get("x-oauth-scopes", "")
    return {"login": body.get("login", ""), "name": body.get("name") or "",
            "scopes": [s.strip() for s in scopes.split(",") if s.strip()]}


def _why(response: httpx.Response) -> str:
    """GitHub's own words, which are usually the useful ones."""
    try:
        body = response.json()
    except ValueError:
        return f"GitHub answered {response.status_code}."
    message = body.get("message") or f"GitHub answered {response.status_code}."
    errors = body.get("errors") or []
    detail = "; ".join(
        e.get("message") or f"{e.get('field', '')} {e.get('code', '')}".strip()
        for e in errors if isinstance(e, dict))
    return f"{message} ({detail})" if detail else message


def create_repository(name: str, description: str = "", private: bool = True,
                      org: str = "") -> dict:
    """
    Make a repository to hold playbooks.

    `private` defaults to True and is passed explicitly on every call, so
    a public repository is something somebody asked for rather than
    something a default drifted into.
    """
    auth = token()
    if not auth:
        raise GitError("No GitHub token is saved.", "no-token")
    if not (name or "").strip():
        raise GitError("A repository needs a name.", "invalid")

    path = f"/orgs/{org}/repos" if org.strip() else "/user/repos"
    response = _call("POST", path, auth, json={
        "name": name.strip(),
        "description": description or "Ansible playbooks from ShellMate",
        "private": bool(private),
        "auto_init": True,   # so there is a branch to commit onto
    })
    if response.status_code not in (200, 201):
        raise GitError(_why(response), "refused")
    body = response.json()
    logger.info("GitHub repository created: %s (private=%s)",
                body.get("full_name"), body.get("private"))
    return {"owner": (body.get("owner") or {}).get("login", ""),
            "repo": body.get("name", ""),
            "private": bool(body.get("private", True)),
            "url": body.get("html_url", "")}


def repository(owner: str, repo: str) -> dict:
    """
    Read a repository that already exists, to check it is there.

    Called before one is stored as the destination. A settings file naming
    a repository nobody can reach turns every later save into a 404
    reported as a permission problem, which is a long way from "that name
    was wrong".
    """
    auth = token()
    if not auth:
        raise GitError("No GitHub token is saved.", "no-token")
    owner, repo = (owner or "").strip(), (repo or "").strip()
    if not owner or not repo:
        raise GitError("Name the owner and the repository.", "invalid")

    response = _call("GET", f"/repos/{owner}/{repo}", auth)
    if response.status_code == 404:
        # 404 is also what GitHub answers for a private repository the
        # token cannot see, so this deliberately does not claim it does
        # not exist. Telling somebody their repository is gone when the
        # truth is that their token cannot read it is the worse error.
        raise GitError(f"{owner}/{repo} could not be read. It may not exist, "
                       "or the token may not have access to it.", "refused")
    if response.status_code != 200:
        raise GitError(_why(response), "refused")
    body = response.json()
    permissions = body.get("permissions") or {}
    if permissions and not permissions.get("push", False):
        raise GitError(f"The token can read {owner}/{repo} but not write to "
                       "it, so a playbook could not be committed.", "forbidden")
    return {"owner": (body.get("owner") or {}).get("login", owner),
            "repo": body.get("name", repo),
            "private": bool(body.get("private", True)),
            "url": body.get("html_url", "")}


def _existing_sha(owner: str, repo: str, path: str, auth: str) -> str:
    """
    The blob sha of the file already there, or "" if there is none.

    GitHub's contents API needs it to update rather than create, and
    omitting it on a file that exists is refused rather than overwritten —
    which is the right way round, but it means asking first.
    """
    response = _call("GET", f"/repos/{owner}/{repo}/contents/{path}", auth)
    if response.status_code == 404:
        return ""
    if response.status_code != 200:
        raise GitError(_why(response), "refused")
    body = response.json()
    if isinstance(body, list):
        raise GitError(f"{path} is a directory in that repository.", "refused")
    return body.get("sha", "")


def commit_playbook(name: str, text: str, message: str = "",
                    owner: str = "", repo: str = "") -> dict:
    """
    Put one playbook in the repository, and nothing else.

    Only ever the file it was given. The inventory is generated from the
    estate and is the whole device list; a repository somebody can make
    public later is the last place it should be.
    """
    auth = token()
    if not auth:
        raise GitError("No GitHub token is saved.", "no-token")

    stored = config()
    owner = (owner or stored["owner"]).strip()
    repo = (repo or stored["repo"]).strip()
    if not owner or not repo:
        raise GitError("No repository is set for playbooks.", "no-repo")

    path = f"{FOLDER}/{name}"
    sha = _existing_sha(owner, repo, path, auth)
    payload = {
        "message": message or f"Update {name} from ShellMate",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    response = _call("PUT", f"/repos/{owner}/{repo}/contents/{path}",
                     auth, json=payload)
    if response.status_code not in (200, 201):
        raise GitError(_why(response), "refused")
    body = response.json()
    commit = body.get("commit") or {}
    return {"committed": True, "created": not sha,
            "url": (body.get("content") or {}).get("html_url", ""),
            "sha": commit.get("sha", "")[:7]}


def commit_tree(files: dict[str, bytes], message: str,
                owner: str = "", repo: str = "", branch: str = "") -> dict:
    """
    Put several files in the repository in **one** commit (Deployments).

    The contents API takes one file per commit, which for a deployment of
    four files would be four commits — and a history where `sites.yml`
    changed in one and `scheme.yml` in the next is a history in which no
    single revision is the deployment as it was run. The Trees API builds
    the whole set against the branch head and commits once.

    Steps, each a request: read the branch head, read its tree, create a
    blob per file, create a tree on top of the base, create a commit, move
    the branch. The branch is moved last and without force, so a commit
    that landed on the branch between reading the head and moving it is
    refused by GitHub rather than overwritten — and reported as such.

    Args:
        files: ``{repository path: bytes}`` — paths already carry
            `runner/project/`; this function knows nothing about layout.
        message: the commit message.
    Returns:
        ``{"sha", "url", "files"}``.
    Raises:
        GitError: no token, no repository, or GitHub refused.
    """
    auth = token()
    if not auth:
        raise GitError("No GitHub token is saved.", "no-token")
    stored = config()
    owner = (owner or stored["owner"]).strip()
    repo = (repo or stored["repo"]).strip()
    if not owner or not repo:
        raise GitError("No repository is set for playbooks.", "no-repo")
    if not files:
        raise GitError("There is nothing to commit.", "refused")

    base = f"/repos/{owner}/{repo}"
    if not branch:
        response = _call("GET", base, auth)
        if response.status_code != 200:
            raise GitError(_why(response), "refused")
        branch = response.json().get("default_branch") or "main"

    response = _call("GET", f"{base}/git/ref/heads/{branch}", auth)
    if response.status_code != 200:
        raise GitError(_why(response), "refused")
    head_sha = response.json()["object"]["sha"]

    response = _call("GET", f"{base}/git/commits/{head_sha}", auth)
    if response.status_code != 200:
        raise GitError(_why(response), "refused")
    base_tree = response.json()["tree"]["sha"]

    entries = []
    for path, content in files.items():
        response = _call("POST", f"{base}/git/blobs", auth, json={
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        if response.status_code != 201:
            raise GitError(f"{path}: {_why(response)}", "refused")
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": response.json()["sha"]})

    response = _call("POST", f"{base}/git/trees", auth,
                     json={"base_tree": base_tree, "tree": entries})
    if response.status_code != 201:
        raise GitError(_why(response), "refused")
    tree_sha = response.json()["sha"]

    response = _call("POST", f"{base}/git/commits", auth, json={
        "message": message, "tree": tree_sha, "parents": [head_sha]})
    if response.status_code != 201:
        raise GitError(_why(response), "refused")
    commit = response.json()
    commit_sha = commit["sha"]

    # No force: a commit that landed on the branch since the head was read
    # makes this a 422, which is the right answer — the alternative is
    # silently discarding somebody else's commit.
    response = _call("PATCH", f"{base}/git/refs/heads/{branch}", auth,
                     json={"sha": commit_sha, "force": False})
    if response.status_code != 200:
        raise GitError("The branch moved while this commit was being built; "
                       "nothing was overwritten. Try again.", "refused")

    logger.info("Committed %d file(s) to %s/%s@%s as %s",
                len(files), owner, repo, branch, commit_sha[:7])
    return {"sha": commit_sha[:7], "url": commit.get("html_url", ""),
            "files": sorted(files), "branch": branch}


def publish(name: str, text: str, message: str = "") -> dict:
    """
    Commit a saved playbook, reporting rather than raising.

    The one entry point the save path uses. Everything that can go wrong
    comes back as `{"ok": False, "why": ..., "code": ...}` because by the
    time this runs the playbook is already on disk, and an exception here
    would turn a successful save into a failed request.

    Whether to publish at all is the caller's decision, not this
    function's — the save endpoint has to make it anyway, to honour a
    per-save override, and two places deciding the same thing is how they
    come to disagree.
    """
    try:
        result = commit_playbook(name, text, message)
        result["ok"] = True
        return result
    except GitError as exc:
        logger.warning("Playbook %s saved locally but not committed: %s", name, exc)
        return {"ok": False, "code": exc.code, "why": str(exc)}
    except Exception as exc:                              # pragma: no cover
        logger.exception("Unexpected failure committing %s", name)
        return {"ok": False, "code": "failed", "why": str(exc)}
