import os
import subprocess
import shutil
import logging

logger = logging.getLogger("grading_agent.tools")

def _resolve_safe_path(repo_root: str, relative_path: str) -> str:
    """Resolves a relative path and ensures it is within the repo_root."""
    repo_root = os.path.abspath(repo_root)
    # Prepend repo_root if relative_path is not already absolute
    if not os.path.isabs(relative_path):
        target_path = os.path.abspath(os.path.join(repo_root, relative_path))
    else:
        target_path = os.path.abspath(relative_path)
        
    if not target_path.startswith(repo_root):
        raise ValueError(f"Access denied: Path {target_path} is outside of repository root {repo_root}")
    return target_path

def clone_repository(url: str, session_id: str) -> str:
    """Clones a GitHub repository to a local temporary directory.
    
    Args:
        url: The GitHub repository URL.
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
        logger.info(f"Repository already cloned for session {session_id}, reusing: {repo_dir}")
        return repo_dir
        
    # Check if URL is actually a local directory
    if os.path.isdir(url):
        logger.info(f"Copying local directory {url} to {repo_dir}")
        try:
            ignore_patterns = shutil.ignore_patterns(
                "cloned_repos", ".venv", "__pycache__", ".git", ".pytest_cache", "artifacts", ".adk"
            )
            shutil.copytree(url, repo_dir, ignore=ignore_patterns)
            logger.info(f"Successfully copied local directory {url}")
            return repo_dir
        except Exception as e:
            logger.error(f"Failed to copy local directory {url}: {str(e)}")
            if os.path.exists(repo_dir):
                shutil.rmtree(repo_dir)
            raise e

    logger.info(f"Cloning repository {url} to {repo_dir}")
    try:
        # Run git clone. We use --depth 1 for faster cloning.
        subprocess.run(
            ["git", "clone", "--depth", "1", url, repo_dir],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        logger.info(f"Successfully cloned {url}")
        return repo_dir
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to clone repository {url}: {e.stderr}")
        if os.path.exists(repo_dir):
            try:
                shutil.rmtree(repo_dir)
            except Exception as cleanup_err:
                logger.warning(f"Failed to clean up directory {repo_dir} after failed clone: {cleanup_err}")
        raise RuntimeError(f"Failed to clone repository: {e.stderr}")
    except Exception as e:
        logger.error(f"Unexpected error cloning repository {url}: {str(e)}")
        if os.path.exists(repo_dir):
            try:
                shutil.rmtree(repo_dir)
            except Exception as cleanup_err:
                logger.warning(f"Failed to clean up directory {repo_dir} after failed clone: {cleanup_err}")
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
        logger.warning(f"Safety check failed: Attempted to delete path outside cloned_repos: {repo_root}")
        return
        
    if os.path.exists(repo_root):
        logger.info(f"Cleaning up repository: {repo_root}")
        shutil.rmtree(repo_root, ignore_errors=True)

def list_directory(repo_root: str, relative_path: str = "") -> list[str]:
    """Lists the contents of a directory within the repository.
    
    Args:
        repo_root: The absolute path to the repository root.
        relative_path: The path to list, relative to the repository root.
        
    Returns:
        A list of file and directory names.
    """
    try:
        target_path = _resolve_safe_path(repo_root, relative_path)
        if not os.path.exists(target_path):
            return [f"Error: Path {relative_path} does not exist."]
        if not os.path.isdir(target_path):
            return [f"Error: Path {relative_path} is not a directory."]
            
        return os.listdir(target_path)
    except Exception as e:
        return [f"Error: {str(e)}"]

def read_file(repo_root: str, file_path: str, max_chars: int = 20000) -> str:
    """Reads the content of a file within the repository.
    
    Args:
        repo_root: The absolute path to the repository root.
        file_path: The path to the file, relative to the repository root.
        max_chars: The maximum number of characters to return (default 20000).
        
    Returns:
        The content of the file, or an error message.
    """
    try:
        target_path = _resolve_safe_path(repo_root, file_path)
        if not os.path.exists(target_path):
            return f"Error: File {file_path} does not exist."
        if not os.path.isfile(target_path):
            return f"Error: Path {file_path} is not a file."
            
        file_size = os.path.getsize(target_path)
        
        with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(max_chars + 1)
            
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n[TRUNCATED... File size: {file_size} bytes. Only first {max_chars} characters shown.]"
        return content
    except Exception as e:
        return f"Error: {str(e)}"

def search_code(repo_root: str, query: str, extension: str = None) -> list[str]:
    """Searches for a query string in all files within the repository.
    
    Args:
        repo_root: The absolute path to the repository root.
        query: The string to search for.
        extension: Optional file extension to restrict the search (e.g., "py", "tf").
        
    Returns:
        A list of matching lines with file paths and line numbers, capped at 50 results.
    """
    results = []
    query_lower = query.lower()
    count = 0
    max_results = 50
    
    try:
        repo_root = os.path.abspath(repo_root)
        for root, _, files in os.walk(repo_root):
            for file in files:
                if extension and not file.endswith(f".{extension}"):
                    continue
                    
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, repo_root)
                
                # Skip common binary/dependency directories
                if any(part in rel_path.split(os.sep) for part in [".git", ".venv", "node_modules", "__pycache__", ".adk"]):
                    continue
                    
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
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
        return [f"Error: {str(e)}"]
