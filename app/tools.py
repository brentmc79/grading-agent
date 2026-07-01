import io
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile

from typing import Annotated
from google.adk.tools import ToolContext
from pydantic import BaseModel, Field

logger = logging.getLogger("grading_agent.tools")

IGNORED_EXTENSIONS = {
    # Images
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "tiff", "webp", "svg",
    # Video
    "mp4", "mkv", "avi", "mov", "webm", "flv",
    # Audio
    "mp3", "wav", "flac", "ogg", "m4a",
    # Archives
    "zip", "tar", "gz", "rar", "7z",
    # Documents
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    # Others
    "pyc", "so", "dll", "exe", "bin", "woff", "woff2", "ttf", "eot",
}



def _resolve_safe_path(repo_root: str, relative_path: str) -> str:
    """Resolves a relative path and ensures it is within the repo_root."""
    repo_root = os.path.abspath(repo_root)
    # Prepend repo_root if relative_path is not already absolute
    if not os.path.isabs(relative_path):
        target_path = os.path.abspath(os.path.join(repo_root, relative_path))
    else:
        target_path = os.path.abspath(relative_path)

    if not target_path.startswith(repo_root):
        raise ValueError(
            f"Access denied: Path {target_path} is outside of repository root {repo_root}"
        )
    return target_path


def clone_repository(url: str, session_id: str) -> str:
    """Clones a GitHub repository to a local temporary directory.

    If the input is a GitHub URL, it downloads the repository as a ZIP archive
    to avoid dependency on the 'git' command-line tool, which may not be available
    in the deployment environment.

    Args:
        url: The GitHub repository URL or a local directory path.
        session_id: The session ID to use for the unique directory name.

    Returns:
        The absolute path to the cloned repository.
    """
    # Create cloned_repos directory in the workspace if it doesn't exist
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cloned_repos_dir = os.path.join(workspace_dir, "cloned_repos")
    os.makedirs(cloned_repos_dir, exist_ok=True)

    repo_dir = os.path.join(cloned_repos_dir, session_id)

    if os.path.exists(repo_dir):
        logger.info(
            f"Repository already cloned for session {session_id}, reusing: {repo_dir}"
        )
        return repo_dir

    # Check if URL is actually a local directory
    if os.path.isdir(url):
        logger.info(f"Copying local directory {url} to {repo_dir}")
        try:
            ignore_patterns = shutil.ignore_patterns(
                "cloned_repos",
                ".venv",
                "__pycache__",
                ".git",
                ".pytest_cache",
                "artifacts",
                ".adk",
            )
            shutil.copytree(url, repo_dir, ignore=ignore_patterns)
            logger.info(f"Successfully copied local directory {url}")
            return repo_dir
        except Exception as e:
            logger.error(f"Failed to copy local directory {url}: {e!s}")
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir)
            raise e

    logger.info(f"Downloading repository from {url} to {repo_dir}")
    try:
        # Parse GitHub URL
        # e.g., https://github.com/brentmc79/grading-agent
        match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
        if not match:
            raise ValueError(
                f"Unsupported URL format: {url}. Only GitHub repository URLs are supported."
            )

        owner, repo = match.group(1), match.group(2)

        # Try main branch first, then master
        branches = ["main", "master"]
        download_success = False
        zip_content = None

        headers = {}
        token = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"
            logger.info("Using GitHub token from environment for authorization.")

        for branch in branches:
            zip_url = (
                f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
            )
            logger.info(f"Trying to download zip from {zip_url}")
            try:
                req = urllib.request.Request(zip_url, headers=headers)
                with urllib.request.urlopen(req) as response:
                    zip_content = response.read()
                    download_success = True
                    logger.info(f"Successfully downloaded zip for branch {branch}")
                    break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.warning(f"Branch {branch} not found (404)")
                    continue
                raise e

        if not download_success:
            raise RuntimeError(
                f"Failed to download repository zip from GitHub. Tried branches: {branches}"
            )

        # Extract zip
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zip_ref:
            # The zip file contains a top-level directory like "repo-branch"
            # We want to extract its contents directly to repo_dir
            first_entry = zip_ref.namelist()[0]
            top_level_dir = first_entry.split("/")[0]

            # Extract all to parent directory
            zip_ref.extractall(cloned_repos_dir)

            extracted_path = os.path.join(cloned_repos_dir, top_level_dir)

            # Rename to repo_dir
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir)
            os.rename(extracted_path, repo_dir)

        logger.info(f"Successfully extracted repository to {repo_dir}")
        return repo_dir

    except Exception as e:
        logger.error(f"Failed to download/extract repository {url}: {e!s}")
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise e


def cleanup_repository(repo_root: str) -> None:
    """Removes the cloned repository directory.

    Args:
        repo_root: The absolute path to the repository root.
    """
    if not repo_root:
        return
    repo_root = os.path.abspath(repo_root)
    # Safety check: make sure we only delete directories under 'cloned_repos'
    workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cloned_repos_dir = os.path.join(workspace_dir, "cloned_repos")

    if not repo_root.startswith(cloned_repos_dir):
        logger.warning(
            f"Safety check failed: Attempted to delete path outside cloned_repos: {repo_root}"
        )
        return

    if os.path.exists(repo_root):
        logger.info(f"Cleaning up repository: {repo_root}")
        shutil.rmtree(repo_root, ignore_errors=True)


def list_directory(
    relative_path: Annotated[
        str,
        Field(
            description="The path to list, relative to the repository root. Use empty string '' for the root directory."
        ),
    ] = "",
    tool_context: ToolContext = None,
) -> list[str]:
    """Lists the contents of a directory within the repository.

    Args:
        relative_path: The path to list, relative to the repository root.
        tool_context: The tool context containing session state.

    Returns:
        A list of file and directory names. If an error occurs, returns a list containing the error message.
        Recovery Instruction: If the path does not exist, verify the path by listing the parent directory.
    """
    try:
        repo_root = tool_context.state.get("local_path")
        if not repo_root:
            return [
                "Error: Repository root not found in state. Please ensure the repository is initialized."
            ]
        target_path = _resolve_safe_path(repo_root, relative_path)
        if not os.path.exists(target_path):
            return [
                f"Error: Path {relative_path} does not exist. Please check the path and try again."
            ]
        if not os.path.isdir(target_path):
            return [
                f"Error: Path {relative_path} is not a directory. Use read_file to read files."
            ]

        return os.listdir(target_path)
    except Exception as e:
        return [f"Error: {e!s}. Ensure the path is within the repository."]


def read_file(
    file_path: Annotated[
        str,
        Field(description="The path to the file, relative to the repository root."),
    ],
    max_chars: Annotated[
        int,
        Field(
            description="The maximum number of characters to return (default 20000)."
        ),
    ] = 20000,
    tool_context: ToolContext = None,
) -> str:
    """Reads the content of a file within the repository.

    Args:
        file_path: The path to the file, relative to the repository root.
        max_chars: The maximum number of characters to return.
        tool_context: The tool context containing session state.

    Returns:
        The content of the file, or an error message.
        Recovery Instruction: If the file does not exist, use list_directory to find the correct file path.
    """
    try:
        repo_root = tool_context.state.get("local_path")
        if not repo_root:
            return "Error: Repository root not found in state. Please ensure the repository is initialized."
        target_path = _resolve_safe_path(repo_root, file_path)
        if not os.path.exists(target_path):
            return f"Error: File {file_path} does not exist. Use list_directory to verify the file location."
        if not os.path.isfile(target_path):
            return f"Error: Path {file_path} is not a file. If it is a directory, use list_directory."

        file_size = os.path.getsize(target_path)

        with open(target_path, encoding="utf-8", errors="ignore") as f:
            content = f.read(max_chars + 1)

        if len(content) > max_chars:
            return (
                content[:max_chars]
                + f"\n\n[TRUNCATED... File size: {file_size} bytes. Only first {max_chars} characters shown. Increase max_chars if you need to read more.]"
            )
        return content
    except Exception as e:
        return f"Error: {e!s}. Ensure the file is a text file and you have permission to read it."


def search_code(
    query: Annotated[str, Field(description="The string to search for.")],
    extension: Annotated[
        str | None,
        Field(
            description="Optional file extension to restrict the search (e.g., 'py', 'tf')."
        ),
    ] = None,
    tool_context: ToolContext = None,
) -> list[str]:
    """Searches for a query string in all files within the repository.

    Args:
        query: The string to search for.
        extension: Optional file extension to restrict the search.
        tool_context: The tool context containing session state.

    Returns:
        A list of matching lines with file paths and line numbers, capped at 50 results.
        Recovery Instruction: If no results are found, try a broader query or check if the file extension is correct.
    """
    results = []
    repo_root = tool_context.state.get("local_path")
    if not repo_root:
        return [
            "Error: Repository root not found in state. Please ensure the repository is initialized."
        ]
    query_lower = query.lower()
    count = 0
    max_results = 50

    try:
        repo_root = os.path.abspath(repo_root)
        for root, _, files in os.walk(repo_root):
            for file in files:
                if extension:
                    if not file.endswith(f".{extension}"):
                        continue
                else:
                    ext = file.split(".")[-1].lower() if "." in file else ""
                    if ext in IGNORED_EXTENSIONS:
                        continue

                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, repo_root)

                # Skip common binary/dependency directories
                if any(
                    part in rel_path.split(os.sep)
                    for part in [".git", ".venv", "node_modules", "__pycache__", ".adk"]
                ):
                    continue

                try:
                    with open(file_path, encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if query_lower in line.lower():
                                results.append(f"{rel_path}:{line_num}: {line.strip()}")
                                count += 1
                                if count >= max_results:
                                    return results
                except Exception:
                    # Ignore files that can't be read
                    continue
        return results
    except Exception as e:
        return [
            f"Error: {e!s}. Try a different query or restrict the search with a file extension."
        ]
