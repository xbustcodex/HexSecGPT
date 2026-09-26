import os
import shutil
import hashlib
import hmac
import json
import fnmatch
import requests
import tempfile
import zipfile
import tarfile
import queue
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from colorama import init, Fore, Style
from packaging import version as pkg_version

# Initialize colorama
init(autoreset=True)

# Custom logging with colors
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': Fore.CYAN,
        'INFO': Fore.GREEN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT
    }
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        record.msg = f"{color}{record.msg}{Fore.RESET}"
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Metadata file written alongside every backup. Read back by rollback().
BACKUP_METADATA_FILE = 'backup_metadata.json'

class SelfUpgradingManager:
    """Advanced self-upgrading manager for Python projects with folder/file management."""
    
    def __init__(self, project_root, config_file='upgrade_config.json'):
        self.project_root = os.path.abspath(project_root)
        self.config = self.load_config(config_file)
        self.current_version = self.get_current_version()
        self.version_cache = {}
        self.upgrade_history = []
        self.stats = {
            'total_folders': 0,
            'total_files': 0,
            'files_updated': 0,
            'files_added': 0,
            'files_deleted': 0,
            'folders_created': 0,
            'folders_deleted': 0,
            'errors': 0
        }
        self.backup_root = os.path.join(self.project_root, '.upgrade_backups')
        self.thread_pool = ThreadPoolExecutor(max_workers=4)
        self.update_queue = queue.Queue()
        self.stop_processing = False
        
    def load_config(self, config_file):
        """Load or create upgrade configuration."""
        default_config = {
            'version_file': 'version.json',
            'update_server': '',
            'backup_enabled': True,
            'parallel_processing': True,
            'max_workers': 4,
            'exclude_patterns': [
                '.git', '__pycache__', '*.pyc', '.env', '.env.*', 'venv',
                '.HexSec', '*.key', '*.pem', '*.pfx', '.upgrade_backups',
            ],
            'version_pattern': r'v(\d+)\.(\d+)\.(\d+)',
            'incremental_updates': True,
            'rollback_enabled': True,
            'max_backups': 5,
            'file_types': ['.py', '.json', '.yaml', '.yml', '.txt', '.md', '.ini', '.cfg'],
            'critical_folders': ['src', 'lib', 'core', 'main'],
            'auto_commit': False,
            'git_enabled': False,
            # HMAC-SHA256 key required to authenticate a downloaded package.
            # Left empty, self-upgrade is disabled entirely.
            'signature_key': '',
            'require_https': True,
        }

        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                # Merge with defaults
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
                return config
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found. Creating default.")
            with open(config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            return default_config

    def _resolve_within_root(self, relative_path, root=None):
        """Resolve a path from untrusted input, refusing anything outside the root.

        Guards against manifest entries like '../../.ssh/authorized_keys' or
        absolute paths, which would otherwise let a malicious upgrade package
        read or clobber arbitrary files.
        """
        root = os.path.abspath(root or self.project_root)
        candidate = os.path.abspath(os.path.join(root, relative_path))
        if candidate != root and not candidate.startswith(root + os.sep):
            raise ValueError(
                f"Refusing path outside project root: {relative_path!r} -> {candidate}"
            )
        return candidate

    def _is_excluded(self, relative_path):
        """True if a path matches any configured exclude pattern."""
        name = os.path.basename(relative_path)
        for pattern in self.config['exclude_patterns']:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_path, pattern):
                return True
        return False
    
    def get_current_version(self):
        """Get current version from version file."""
        version_file = os.path.join(self.project_root, self.config['version_file'])
        try:
            with open(version_file, 'r') as f:
                data = json.load(f)
                return data.get('version', '0.0.0')
        except:
            return '0.0.0'
    
    def parse_version(self, version_str):
        """Parse version string to tuple."""
        import re
        match = re.search(self.config['version_pattern'], version_str)
        if match:
            return tuple(int(x) for x in match.groups())
        return (0, 0, 0)
    
    def compare_versions(self, v1, v2):
        """Compare two version strings."""
        try:
            return pkg_version.parse(v1) < pkg_version.parse(v2)
        except:
            v1_parts = self.parse_version(v1)
            v2_parts = self.parse_version(v2)
            return v1_parts < v2_parts
    
    def get_available_upgrades(self):
        """Fetch available upgrades from update server."""
        update_server = self.config.get('update_server', '')
        if not update_server:
            logger.error(
                f"{Fore.RED}❌ No 'update_server' configured in upgrade_config.json"
            )
            return None
        if self.config.get('require_https', True) and not update_server.startswith('https://'):
            logger.error(f"{Fore.RED}❌ update_server must use https://")
            return None
        try:
            response = requests.get(
                f"{update_server}/versions",
                params={'current': self.current_version},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch upgrades: {e}")
        return None
    
    def scan_project_structure(self):
        """Scan entire project structure with parallel processing."""
        logger.info(f"{Fore.CYAN}🔍 Scanning project: {self.project_root}")
        
        structure = {
            'folders': [],
            'files': [],
            'file_hashes': {},
            'folder_structure': {}
        }
        
        def should_exclude(path):
            rel = os.path.relpath(path, self.project_root)
            return self._is_excluded(rel)

        # Walk through directory
        structure['folder_structure']['.'] = []
        for root, dirs, files in os.walk(self.project_root):
            # Skip excluded directories (in-place so os.walk prunes them)
            dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]

            rel_root = os.path.relpath(root, self.project_root)
            if rel_root != '.':
                structure['folders'].append(rel_root)
                structure['folder_structure'][rel_root] = []

            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, self.project_root)

                if should_exclude(file_path):
                    continue

                # Check if file type is monitored
                file_ext = os.path.splitext(file)[1].lower()
                if file_ext in self.config['file_types']:
                    structure['files'].append(rel_path)
                    structure['folder_structure'][rel_root].append(file)

                    # Calculate hash for version tracking
                    try:
                        with open(file_path, 'rb') as f:
                            file_hash = hashlib.sha256(f.read()).hexdigest()
                            structure['file_hashes'][rel_path] = file_hash
                    except OSError:
                        pass
        
        self.stats['total_folders'] = len(structure['folders'])
        self.stats['total_files'] = len(structure['files'])
        
        logger.info(f"{Fore.GREEN}✓ Found {self.stats['total_folders']} folders and {self.stats['total_files']} files")
        return structure
    
    def create_backup(self):
        """Create full project backup before upgrade."""
        if not self.config['backup_enabled']:
            return None
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(self.backup_root, f'backup_{timestamp}')
        
        try:
            os.makedirs(backup_dir, exist_ok=True)
            
            logger.info(f"{Fore.CYAN}💾 Creating backup...")
            
            # Copy project, honouring exclude_patterns so secrets
            # (.HexSec, .env, keys) are never duplicated into backups.
            for root, dirs, files in os.walk(self.project_root):
                dirs[:] = [d for d in dirs if not self._is_excluded(
                    os.path.relpath(os.path.join(root, d), self.project_root)
                )]

                rel_path = os.path.relpath(root, self.project_root)
                target_dir = os.path.join(backup_dir, rel_path)
                os.makedirs(target_dir, exist_ok=True)

                for file in files:
                    if self._is_excluded(os.path.join(rel_path, file)):
                        continue
                    src = os.path.join(root, file)
                    dst = os.path.join(target_dir, file)
                    shutil.copy2(src, dst)
            
            # Save metadata
            metadata = {
                'timestamp': timestamp,
                'version': self.current_version,
                'files_count': self.stats['total_files'],
                'folders_count': self.stats['total_folders']
            }
            with open(os.path.join(backup_dir, BACKUP_METADATA_FILE), 'w') as f:
                json.dump(metadata, f, indent=2)
            
            logger.info(f"{Fore.GREEN}✓ Backup created: {backup_dir}")
            
            # Cleanup old backups
            self.cleanup_old_backups()
            
            return backup_dir
            
        except Exception as e:
            logger.error(f"{Fore.RED}❌ Backup failed: {e}")
            return None
    
    def cleanup_old_backups(self):
        """Keep only the maximum number of backups."""
        max_backups = self.config['max_backups']
        try:
            backups = sorted([
                os.path.join(self.backup_root, d) 
                for d in os.listdir(self.backup_root)
                if os.path.isdir(os.path.join(self.backup_root, d))
            ], key=os.path.getctime, reverse=True)
            
            for backup in backups[max_backups:]:
                shutil.rmtree(backup)
                logger.info(f"Removed old backup: {backup}")
        except:
            pass
    
    def _verify_package_signature(self, package_path, signature_path):
        """Verify HMAC-SHA256 signature of the downloaded package.

        Refuses to proceed unless the package authenticates against the
        configured key, so a hijacked or spoofed update server cannot hand
        this tool arbitrary code.
        """
        key = self.config.get('signature_key', '')
        if not key:
            logger.error(
                f"{Fore.RED}❌ No 'signature_key' configured - refusing to install unverified code"
            )
            return False
        if not signature_path or not os.path.exists(signature_path):
            logger.error(f"{Fore.RED}❌ Missing signature file - refusing to install unverified code")
            return False

        with open(package_path, 'rb') as f:
            digest = hmac.new(key.encode('utf-8'), f.read(), hashlib.sha256).hexdigest()

        with open(signature_path, 'r') as f:
            expected = f.read().strip()

        if not hmac.compare_digest(digest, expected):
            logger.error(f"{Fore.RED}❌ Signature mismatch - package rejected (possible tampering)")
            return False

        logger.info(f"{Fore.GREEN}✓ Package signature verified")
        return True

    def download_upgrade_package(self, version):
        """Download upgrade package and its detached signature for a version."""
        update_server = self.config.get('update_server', '')
        if not update_server:
            logger.error(
                f"{Fore.RED}❌ No 'update_server' configured in upgrade_config.json"
            )
            return None
        if self.config.get('require_https', True) and not update_server.startswith('https://'):
            logger.error(f"{Fore.RED}❌ update_server must use https://")
            return None

        try:
            url = f"{update_server}/download/{version}"
            logger.info(f"{Fore.CYAN}📥 Downloading upgrade package v{version}...")

            response = requests.get(url, stream=True, timeout=120)
            if response.status_code != 200:
                logger.error(f"{Fore.RED}❌ Failed to download upgrade package")
                return None

            # Save to temp file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(temp_file.name, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress = (downloaded / total_size) * 100
                        print(f"\r{Fore.CYAN}  Downloading: {Fore.WHITE}{progress:.1f}%", end='')

            print()  # New line

            # Fetch detached signature alongside the archive
            sig_response = requests.get(f"{url}.sig", timeout=30)
            if sig_response.status_code != 200:
                logger.error(f"{Fore.RED}❌ Failed to download package signature")
                return None
            sig_path = os.path.join(tempfile.gettempdir(), f"upgrade_{version}.sig")
            with open(sig_path, 'w') as f:
                f.write(sig_response.text.strip())

            if not self._verify_package_signature(temp_file.name, sig_path):
                os.unlink(temp_file.name)
                os.unlink(sig_path)
                return None

            logger.info(f"{Fore.GREEN}✓ Downloaded and verified upgrade package")
            return temp_file.name

        except Exception as e:
            logger.error(f"{Fore.RED}❌ Download failed: {e}")
            return None

    def _safe_extract_zip(self, zip_ref, extract_dir):
        """Extract a zip, rejecting any member that escapes extract_dir."""
        extract_root = os.path.abspath(extract_dir)
        for member in zip_ref.infolist():
            target = os.path.abspath(os.path.join(extract_root, member.filename))
            if target != extract_root and not target.startswith(extract_root + os.sep):
                raise ValueError(f"Unsafe zip entry: {member.filename}")
            # Refuse symlinks that could redirect writes elsewhere.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Refusing symlink in archive: {member.filename}")
        zip_ref.extractall(extract_root)

    def _safe_extract_tar(self, tar_ref, extract_dir):
        """Extract a tar.gz, rejecting traversal, absolute paths and links."""
        extract_root = os.path.abspath(extract_dir)
        for member in tar_ref.getmembers():
            target = os.path.abspath(os.path.join(extract_root, member.name))
            if target != extract_root and not target.startswith(extract_root + os.sep):
                raise ValueError(f"Unsafe tar entry: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Refusing link in archive: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Refusing special file in archive: {member.name}")
        tar_ref.extractall(extract_root, filter='data')

    def extract_upgrade_package(self, package_path):
        """Extract upgrade package to a temp directory, refusing unsafe entries."""
        try:
            extract_dir = tempfile.mkdtemp(prefix='upgrade_')

            if package_path.endswith('.zip'):
                with zipfile.ZipFile(package_path, 'r') as zip_ref:
                    self._safe_extract_zip(zip_ref, extract_dir)
            elif package_path.endswith('.tar.gz'):
                with tarfile.open(package_path, 'r:gz') as tar_ref:
                    self._safe_extract_tar(tar_ref, extract_dir)
            else:
                logger.error(f"{Fore.RED}❌ Unknown package format")
                return None

            logger.info(f"{Fore.GREEN}✓ Extracted upgrade package to: {extract_dir}")
            return extract_dir

        except Exception as e:
            logger.error(f"{Fore.RED}❌ Extraction failed: {e}")
            return None
    
    def apply_upgrade_patch(self, upgrade_dir, version):
        """Apply upgrade patch to project with parallel processing."""
        logger.info(f"{Fore.CYAN}🔧 Applying upgrade to v{version}...")
        
        # Parse upgrade manifest
        manifest_file = os.path.join(upgrade_dir, 'manifest.json')
        if not os.path.exists(manifest_file):
            logger.error(f"{Fore.RED}❌ Manifest file not found")
            return False
        
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
        
        # Prepare tasks for parallel processing
        tasks = []
        
        # Every path below comes from the downloaded manifest and is therefore
        # untrusted. _resolve_within_root raises on any path that would escape
        # the project directory, so a malicious package cannot write, delete or
        # create anything outside it.
        try:
            # Create new folders
            for folder in manifest.get('folders_to_create', []):
                target_folder = self._resolve_within_root(folder)
                tasks.append(('create_folder', target_folder))

            # Add/update files
            for file_info in manifest.get('files_to_add', []):
                src = self._resolve_within_root(file_info['source'], upgrade_dir)
                dst = self._resolve_within_root(file_info['destination'])
                tasks.append(('add_file', src, dst, file_info.get('hash')))

            # Delete files
            for file_path in manifest.get('files_to_delete', []):
                full_path = self._resolve_within_root(file_path)
                tasks.append(('delete_file', full_path))

            # Update existing files
            for file_info in manifest.get('files_to_update', []):
                src = self._resolve_within_root(file_info['source'], upgrade_dir)
                dst = self._resolve_within_root(file_info['destination'])
                tasks.append(('update_file', src, dst, file_info.get('hash')))
        except ValueError as e:
            logger.error(f"{Fore.RED}❌ Rejected malicious manifest: {e}")
            return False
        
        # Process tasks in parallel
        if self.config['parallel_processing']:
            with ThreadPoolExecutor(max_workers=self.config['max_workers']) as executor:
                futures = []
                for task in tasks:
                    futures.append(executor.submit(self.process_task, task))
                
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=60)
                        if result:
                            self.update_queue.put(result)
                    except Exception as e:
                        logger.error(f"{Fore.RED}❌ Task failed: {e}")
                        self.stats['errors'] += 1
        else:
            # Sequential processing
            for task in tasks:
                result = self.process_task(task)
                if result:
                    self.update_queue.put(result)
        
        # Update version file
        version_file = os.path.join(self.project_root, self.config['version_file'])
        version_data = {
            'version': version,
            'updated_at': datetime.now().isoformat(),
            'previous_version': self.current_version,
            'upgrade_type': manifest.get('upgrade_type', 'minor')
        }
        with open(version_file, 'w') as f:
            json.dump(version_data, f, indent=2)
        
        # Record upgrade
        self.upgrade_history.append({
            'version': version,
            'timestamp': datetime.now().isoformat(),
            'stats': self.stats.copy()
        })
        
        self.current_version = version
        logger.info(f"{Fore.GREEN}✅ Successfully upgraded to v{version}")
        return True
    
    def process_task(self, task):
        """Process a single upgrade task."""
        task_type = task[0]
        try:
            if task_type == 'create_folder':
                folder_path = task[1]
                os.makedirs(folder_path, exist_ok=True)
                self.stats['folders_created'] += 1
                return {'type': 'folder_created', 'path': folder_path}
            
            elif task_type in ['add_file', 'update_file']:
                src, dst, file_hash = task[1], task[2], task[3]

                # Verify the source BEFORE touching the destination. A mismatch
                # aborts the file instead of leaving tampered content on disk.
                if file_hash:
                    with open(src, 'rb') as f:
                        src_hash = hashlib.sha256(f.read()).hexdigest()
                    if src_hash != file_hash:
                        logger.error(f"{Fore.RED}❌ Hash mismatch for {dst} - file not written")
                        self.stats['errors'] += 1
                        return None

                # Create destination directory if needed
                os.makedirs(os.path.dirname(dst), exist_ok=True)

                # Backup existing file
                if os.path.exists(dst) and self.config['backup_enabled']:
                    backup_path = dst + '.backup'
                    shutil.copy2(dst, backup_path)

                # Copy new file
                shutil.copy2(src, dst)

                self.stats['files_updated' if task_type == 'update_file' else 'files_added'] += 1
                return {'type': 'file_updated', 'path': dst}
            
            elif task_type == 'delete_file':
                file_path = task[1]
                if os.path.exists(file_path):
                    # Backup before deletion
                    if self.config['backup_enabled']:
                        backup_path = file_path + '.deleted'
                        shutil.copy2(file_path, backup_path)
                    
                    os.remove(file_path)
                    self.stats['files_deleted'] += 1
                    return {'type': 'file_deleted', 'path': file_path}
            
        except Exception as e:
            logger.error(f"{Fore.RED}❌ Task failed: {task} - {e}")
            self.stats['errors'] += 1
            return None
        
        return None
    
    def git_commit(self, version):
        """Commit changes to git if enabled and GitPython is available."""
        if not self.config['git_enabled']:
            return

        try:
            import git
        except ImportError:
            logger.warning(
                f"{Fore.YELLOW}⚠️ git_enabled is set but GitPython is not installed "
                f"(pip install GitPython) - skipping commit"
            )
            return

        try:
            repo = git.Repo(self.project_root)

            # Add all changes
            repo.git.add(A=True)

            # Commit with upgrade message
            commit_msg = f"Upgrade to v{version}\n\nProject upgraded from v{self.current_version} to v{version}"
            if self.upgrade_history:
                commit_msg += f"\n\nFiles updated: {self.stats['files_updated']}"
                commit_msg += f"\nFiles added: {self.stats['files_added']}"
                commit_msg += f"\nFiles deleted: {self.stats['files_deleted']}"

            repo.index.commit(commit_msg)
            logger.info(f"{Fore.GREEN}✓ Git commit created")

        except Exception as e:
            logger.warning(f"{Fore.YELLOW}⚠️ Git commit failed: {e}")
    
    def rollback(self, backup_dir=None):
        """Rollback to previous version."""
        if not self.config['rollback_enabled']:
            logger.warning(f"{Fore.YELLOW}⚠️ Rollback is disabled")
            return False
        
        try:
            # Find latest backup if not specified
            if not backup_dir:
                backups = sorted([
                    os.path.join(self.backup_root, d)
                    for d in os.listdir(self.backup_root)
                    if os.path.isdir(os.path.join(self.backup_root, d))
                ], key=os.path.getctime, reverse=True)
                
                if not backups:
                    logger.error(f"{Fore.RED}❌ No backups available")
                    return False
                backup_dir = backups[0]
            
            logger.info(f"{Fore.CYAN}🔄 Rolling back to: {backup_dir}")
            
            # Restore backup. The backup root maps onto the project root, so it
            # must NOT be skipped - otherwise root-level files (HexSecGPT.py,
            # version.json, ...) are never restored.
            for root, dirs, files in os.walk(backup_dir):
                rel_path = os.path.relpath(root, backup_dir)
                target_path = os.path.join(self.project_root, rel_path)
                os.makedirs(target_path, exist_ok=True)

                for file in files:
                    if file == BACKUP_METADATA_FILE:
                        continue
                    src = os.path.join(root, file)
                    dst = os.path.join(target_path, file)
                    shutil.copy2(src, dst)

            # Restore version
            metadata_file = os.path.join(backup_dir, BACKUP_METADATA_FILE)
            if os.path.exists(metadata_file):
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)
                # Older backups used a different schema with no 'version' key.
                if 'version' in metadata:
                    self.current_version = metadata['version']

                    # Update version file
                    version_file = os.path.join(self.project_root, self.config['version_file'])
                    with open(version_file, 'w') as f:
                        json.dump({
                            'version': self.current_version,
                            'restored_at': datetime.now().isoformat(),
                            'restored_from': backup_dir
                        }, f, indent=2)
                else:
                    logger.warning(
                        f"{Fore.YELLOW}⚠️ Backup metadata has no 'version' field - "
                        f"version file left unchanged"
                    )
            
            logger.info(f"{Fore.GREEN}✅ Rollback successful to v{self.current_version}")
            return True
            
        except Exception as e:
            logger.error(f"{Fore.RED}❌ Rollback failed: {e}")
            return False
    
    def perform_upgrade(self, target_version=None):
        """Main upgrade process with full scanning and incremental updates."""
        self.print_header("PROJECT UPGRADE MANAGER", "═")
        
        print(f"{Fore.CYAN}📂 Project: {Fore.WHITE}{os.path.basename(self.project_root)}")
        print(f"{Fore.CYAN}📌 Current Version: {Fore.WHITE}v{self.current_version}")
        
        # Scan current project structure
        self.scan_project_structure()
        
        # Get available upgrades
        if target_version:
            upgrade_info = {'version': target_version}
        else:
            upgrade_info = self.get_available_upgrades()
            if not upgrade_info:
                logger.error(f"{Fore.RED}❌ No upgrade information available")
                return False
        
        latest_version = upgrade_info.get('version')
        if not latest_version:
            logger.error(f"{Fore.RED}❌ No version specified")
            return False
        
        # Check if upgrade is needed
        if not self.compare_versions(self.current_version, latest_version):
            logger.info(f"{Fore.GREEN}✅ Already at latest version v{self.current_version}")
            return True
        
        logger.info(f"{Fore.CYAN}⬆️ Upgrade available: v{self.current_version} → v{latest_version}")
        
        # Show upgrade details
        print(f"\n{Fore.CYAN}📋 Upgrade Details:")
        print(f"  {Fore.WHITE}• Type: {upgrade_info.get('type', 'Minor')}")
        print(f"  {Fore.WHITE}• Changes: {upgrade_info.get('changes', 'Various improvements')}")
        print(f"  {Fore.WHITE}• Files affected: {upgrade_info.get('files_affected', 'Unknown')}")
        
        # Ask for confirmation
        print(f"\n{Fore.YELLOW}⚠️ This will update {self.stats['total_files']} files in {self.stats['total_folders']} folders")
        choice = self.confirm_action("Proceed with upgrade?", default='n')
        
        if not choice:
            logger.info(f"{Fore.YELLOW}⚠️ Upgrade cancelled")
            return False
        
        # Create backup
        backup_dir = self.create_backup()
        if not backup_dir:
            logger.error(f"{Fore.RED}❌ Backup failed, aborting upgrade")
            return False
        
        try:
            # Download upgrade package
            package_path = self.download_upgrade_package(latest_version)
            if not package_path:
                logger.error(f"{Fore.RED}❌ Failed to download upgrade package")
                self.rollback(backup_dir)
                return False
            
            # Extract package
            upgrade_dir = self.extract_upgrade_package(package_path)
            if not upgrade_dir:
                logger.error(f"{Fore.RED}❌ Failed to extract upgrade package")
                self.rollback(backup_dir)
                return False
            
            # Apply upgrade
            success = self.apply_upgrade_patch(upgrade_dir, latest_version)
            
            if success:
                # Git commit if enabled
                if self.config['auto_commit']:
                    self.git_commit(latest_version)
                
                # Clean up temp files
                try:
                    os.unlink(package_path)
                    shutil.rmtree(upgrade_dir)
                except:
                    pass
                
                self.print_summary()
                return True
            else:
                # Rollback on failure
                logger.error(f"{Fore.RED}❌ Upgrade failed, rolling back...")
                self.rollback(backup_dir)
                return False
                
        except Exception as e:
            logger.error(f"{Fore.RED}❌ Upgrade process error: {e}")
            self.rollback(backup_dir)
            return False
    
    def confirm_action(self, message, default='y'):
        """Get user confirmation."""
        valid_responses = {'y': True, 'yes': True, 'n': False, 'no': False}
        
        prompt = f"{Fore.YELLOW}❓ {message} {Fore.CYAN}[y/n] {Fore.WHITE}(default: {default}){Fore.RESET} "
        
        while True:
            response = input(prompt).strip().lower()
            if not response:
                response = default
                
            if response in valid_responses:
                return valid_responses[response]
            else:
                print(f"{Fore.RED}✗ Invalid response. Please enter y or n.{Style.RESET_ALL}")
    
    def print_header(self, text, char='='):
        """Print a formatted header."""
        print(f"\n{Fore.CYAN}{Style.BRIGHT}{char * 60}")
        print(f"{Fore.CYAN}{Style.BRIGHT}{text.center(60)}")
        print(f"{Fore.CYAN}{Style.BRIGHT}{char * 60}{Style.RESET_ALL}\n")
    
    def print_summary(self):
        """Print detailed upgrade summary."""
        self.print_header("UPGRADE SUMMARY", "═")
        
        print(f"{Fore.CYAN}📊 Statistics:")
        print(f"  {Fore.WHITE}Folders processed: {self.stats['total_folders']}")
        print(f"  {Fore.WHITE}Files processed: {self.stats['total_files']}")
        print(f"  {Fore.GREEN}Files updated: {self.stats['files_updated']}")
        print(f"  {Fore.GREEN}Files added: {self.stats['files_added']}")
        print(f"  {Fore.RED}Files deleted: {self.stats['files_deleted']}")
        print(f"  {Fore.GREEN}Folders created: {self.stats['folders_created']}")
        print(f"  {Fore.RED}Folders deleted: {self.stats['folders_deleted']}")
        print(f"  {Fore.RED}Errors: {self.stats['errors']}")
        
        if self.upgrade_history:
            print(f"\n{Fore.CYAN}📝 Upgrade History:")
            for i, entry in enumerate(self.upgrade_history[-5:], 1):
                print(f"  {Fore.YELLOW}{i}. v{entry['version']} - {entry['timestamp']}")
        
        # Save upgrade history
        history_file = os.path.join(self.project_root, '.upgrade_history.json')
        with open(history_file, 'w') as f:
            json.dump(self.upgrade_history, f, indent=2)
        
        print(f"\n{Fore.GREEN}✅ Upgrade completed successfully to v{self.current_version}")
    
    def self_upgrade_loop(self, check_interval=3600, auto_apply=False):
        """Poll for updates.

        Detects and reports new versions only. Applying one still requires
        explicit confirmation via perform_upgrade(), so this loop can never
        install code unattended.
        """
        logger.info(f"{Fore.CYAN}🔄 Starting update monitoring loop...")

        while not self.stop_processing:
            try:
                upgrade_info = self.get_available_upgrades()
                if upgrade_info and self.compare_versions(self.current_version, upgrade_info.get('version')):
                    logger.info(f"{Fore.GREEN}⬆️ New version available: v{upgrade_info['version']}")

                    if not auto_apply:
                        logger.info(
                            f"{Fore.CYAN}ℹ️  Run 'upgrade {upgrade_info['version']}' to apply it"
                        )
                    else:
                        success = self.perform_upgrade(upgrade_info['version'])
                        if success:
                            logger.info(f"{Fore.GREEN}✅ Self-upgrade successful")
                        else:
                            logger.error(f"{Fore.RED}❌ Self-upgrade failed")
                
                # Wait before next check
                for i in range(check_interval):
                    if self.stop_processing:
                        break
                    time.sleep(1)
                    
            except KeyboardInterrupt:
                logger.info(f"{Fore.YELLOW}⚠️ Upgrade loop interrupted")
                break
            except Exception as e:
                logger.error(f"{Fore.RED}❌ Error in upgrade loop: {e}")
                time.sleep(60)  # Wait before retrying
    
    def emergency_recovery(self):
        """Emergency recovery function if upgrade fails completely."""
        logger.info(f"{Fore.YELLOW}🚨 Emergency recovery initiated...")
        
        # Find latest backup
        try:
            backups = sorted([
                os.path.join(self.backup_root, d)
                for d in os.listdir(self.backup_root)
                if os.path.isdir(os.path.join(self.backup_root, d))
            ], key=os.path.getctime, reverse=True)
            
            if backups:
                latest_backup = backups[0]
                logger.info(f"{Fore.CYAN}📂 Found backup: {latest_backup}")
                
                # Restore from backup, including root-level files.
                for root, dirs, files in os.walk(latest_backup):
                    rel_path = os.path.relpath(root, latest_backup)
                    target_path = os.path.join(self.project_root, rel_path)
                    os.makedirs(target_path, exist_ok=True)

                    for file in files:
                        if file == BACKUP_METADATA_FILE:
                            continue
                        src = os.path.join(root, file)
                        dst = os.path.join(target_path, file)
                        shutil.copy2(src, dst)
                
                logger.info(f"{Fore.GREEN}✅ Emergency recovery successful")
                return True
            else:
                logger.error(f"{Fore.RED}❌ No backups found for recovery")
                return False
                
        except Exception as e:
            logger.error(f"{Fore.RED}❌ Emergency recovery failed: {e}")
            return False

class UpgradeManagerCLI:
    """Command-line interface for the upgrade manager."""
    
    def __init__(self, project_root):
        self.manager = SelfUpgradingManager(project_root)
        self.commands = {
            'upgrade': self.cmd_upgrade,
            'rollback': self.cmd_rollback,
            'status': self.cmd_status,
            'history': self.cmd_history,
            'monitor': self.cmd_monitor,
            'recover': self.cmd_recover,
            'help': self.cmd_help,
            'exit': self.cmd_exit
        }
    
    def run(self):
        """Run the CLI interface."""
        self.manager.print_header("SELF-UPGRADE MANAGER CLI", "═")
        print(f"{Fore.CYAN}Commands: upgrade, rollback, status, history, monitor, recover, help, exit{Style.RESET_ALL}")
        
        while True:
            try:
                cmd = input(f"\n{Fore.GREEN}➜ {Fore.WHITE}").strip().lower()
                
                if not cmd:
                    continue
                
                if cmd in self.commands:
                    self.commands[cmd]()
                else:
                    print(f"{Fore.RED}✗ Unknown command: {cmd}")
                    print(f"{Fore.CYAN}Available commands: {', '.join(self.commands.keys())}")
                    
            except KeyboardInterrupt:
                print(f"\n{Fore.YELLOW}⚠️ Exiting...")
                break
            except Exception as e:
                print(f"{Fore.RED}❌ Error: {e}")
    
    def cmd_upgrade(self):
        """Upgrade to latest version."""
        target = input(f"{Fore.CYAN}Target version (press Enter for latest): {Fore.WHITE}").strip()
        if not target:
            target = None
        self.manager.perform_upgrade(target)
    
    def cmd_rollback(self):
        """Rollback to previous version."""
        self.manager.rollback()
    
    def cmd_status(self):
        """Show current status."""
        print(f"\n{Fore.CYAN}📊 Current Status:")
        print(f"  {Fore.WHITE}Project: {os.path.basename(self.manager.project_root)}")
        print(f"  {Fore.WHITE}Version: v{self.manager.current_version}")
        print(f"  {Fore.WHITE}Files: {self.manager.stats['total_files']}")
        print(f"  {Fore.WHITE}Folders: {self.manager.stats['total_folders']}")
    
    def cmd_history(self):
        """Show upgrade history."""
        history_file = os.path.join(self.manager.project_root, '.upgrade_history.json')
        try:
            with open(history_file, 'r') as f:
                history = json.load(f)
                print(f"\n{Fore.CYAN}📝 Upgrade History:")
                for i, entry in enumerate(history, 1):
                    print(f"  {Fore.YELLOW}{i}. v{entry['version']} - {entry['timestamp']}")
                    print(f"     {Fore.WHITE}Files updated: {entry['stats']['files_updated']}")
        except:
            print(f"{Fore.YELLOW}No upgrade history found")
    
    def cmd_monitor(self):
        """Watch for updates (reports only; never installs unattended)."""
        try:
            self.manager.self_upgrade_loop(30)
        except KeyboardInterrupt:
            self.manager.stop_processing = True
            print(f"\n{Fore.YELLOW}⚠️ Monitoring stopped")
    
    def cmd_recover(self):
        """Emergency recovery."""
        self.manager.emergency_recovery()
    
    def cmd_help(self):
        """Show help."""
        print(f"\n{Fore.CYAN}📖 Available Commands:")
        for cmd, func in self.commands.items():
            print(f"  {Fore.YELLOW}{cmd:12}{Fore.WHITE}{func.__doc__ or ''}")
    
    def cmd_exit(self):
        """Exit the CLI."""
        print(f"{Fore.CYAN}👋 Goodbye!")
        raise KeyboardInterrupt

def main():
    """Main entry point."""
    # Configure project root
    project_root = os.getcwd()  # Or specify path
    
    # Check if running in project directory
    if not os.path.exists(os.path.join(project_root, 'version.json')):
        print(f"{Fore.YELLOW}⚠️ No version.json found in current directory")
        create = input(f"{Fore.CYAN}Create version.json? [y/n]: {Fore.WHITE}")
        if create.lower() == 'y':
            with open(os.path.join(project_root, 'version.json'), 'w') as f:
                json.dump({'version': '1.0.0'}, f, indent=2)
            print(f"{Fore.GREEN}✓ Created version.json with version 1.0.0")
    
    # Run CLI
    cli = UpgradeManagerCLI(project_root)
    cli.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}⚠️  Program terminated by user{Style.RESET_ALL}")
    except Exception as e:
        print(f"\n{Fore.RED}❌ Unexpected error: {e}{Style.RESET_ALL}")
        import traceback
        traceback.print_exc()