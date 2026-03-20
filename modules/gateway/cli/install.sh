#!/usr/bin/env bash
#
# install.sh - Install Bedrock Gateway CLI tools
#
# This script installs bg-auth.sh to ~/bin/ and ensures it's in the PATH.
#
# Usage:
#   ./install.sh                 # Install to ~/bin/
#   ./install.sh --prefix /path  # Install to custom directory
#   ./install.sh --uninstall     # Remove installation
#
# Requirements:
#   - curl (for API calls)
#   - jq (for JSON parsing)
#   - aws cli (for credential management)
#

set -euo pipefail

# Constants
readonly VERSION="1.0.0"
readonly SCRIPT_NAME="bg-install"
readonly DEFAULT_INSTALL_DIR="$HOME/bin"

# Global variables
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
UNINSTALL=false
FORCE=false

# Colors for output (if terminal supports it)
if [[ -t 1 ]]; then
    readonly RED='\033[0;31m'
    readonly GREEN='\033[0;32m'
    readonly YELLOW='\033[1;33m'
    readonly BLUE='\033[0;34m'
    readonly NC='\033[0m' # No Color
else
    readonly RED=''
    readonly GREEN=''
    readonly YELLOW=''
    readonly BLUE=''
    readonly NC=''
fi

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Print usage information
usage() {
    cat << EOF
$SCRIPT_NAME v$VERSION - Install Bedrock Gateway CLI tools

Usage: $SCRIPT_NAME [OPTIONS]

Options:
  --prefix DIR    Install to custom directory (default: ~/bin/)
  --uninstall     Remove bg-auth.sh from installation directory
  --force         Overwrite existing installation without prompting
  -h, --help      Show this help message
  -v, --version   Show version

Examples:
  # Default installation
  ./install.sh

  # Install to custom directory
  ./install.sh --prefix /usr/local/bin

  # Uninstall
  ./install.sh --uninstall
EOF
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prefix)
                if [[ -z "${2:-}" ]]; then
                    log_error "--prefix requires a directory argument"
                    exit 1
                fi
                INSTALL_DIR="$2"
                shift 2
                ;;
            --uninstall)
                UNINSTALL=true
                shift
                ;;
            --force)
                FORCE=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            -v|--version)
                echo "$SCRIPT_NAME v$VERSION"
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done
}

# Check for required dependencies
check_dependencies() {
    local missing=()
    local dep_status=true

    log_info "Checking dependencies..."

    for cmd in curl jq aws; do
        if command -v "$cmd" &>/dev/null; then
            log_success "$cmd found: $(command -v "$cmd")"
        else
            log_error "$cmd not found"
            missing+=("$cmd")
            dep_status=false
        fi
    done

    if [[ "$dep_status" == "false" ]]; then
        echo ""
        log_error "Missing required dependencies: ${missing[*]}"
        echo ""
        log_info "Install missing dependencies:"
        for dep in "${missing[@]}"; do
            case "$dep" in
                curl)
                    echo "  - curl: sudo yum install curl  (or: sudo apt install curl)"
                    ;;
                jq)
                    echo "  - jq: sudo yum install jq  (or: sudo apt install jq)"
                    ;;
                aws)
                    echo "  - aws: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
                    ;;
            esac
        done
        exit 1
    fi

    log_success "All dependencies found"
}

# Get the directory where this script is located
get_script_dir() {
    local source="${BASH_SOURCE[0]}"
    local dir

    # Resolve symlinks
    while [[ -L "$source" ]]; do
        dir="$(cd -P "$(dirname "$source")" && pwd)"
        source="$(readlink "$source")"
        [[ "$source" != /* ]] && source="$dir/$source"
    done

    cd -P "$(dirname "$source")" && pwd
}

# Install bg-auth.sh
install_bg_auth() {
    local script_dir
    script_dir="$(get_script_dir)"
    local source_file="$script_dir/bg-auth.sh"
    local target_file="$INSTALL_DIR/bg-auth.sh"

    log_info "Installing bg-auth.sh..."

    # Check source file exists
    if [[ ! -f "$source_file" ]]; then
        log_error "Source file not found: $source_file"
        log_error "Make sure you're running this script from the cli/ directory"
        exit 1
    fi

    # Create install directory if it doesn't exist
    if [[ ! -d "$INSTALL_DIR" ]]; then
        log_info "Creating directory: $INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
    fi

    # Check if target already exists
    if [[ -f "$target_file" ]] && [[ "$FORCE" != "true" ]]; then
        log_warn "bg-auth.sh already exists at $target_file"
        read -r -p "Overwrite? [y/N] " response
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            log_info "Installation cancelled"
            exit 0
        fi
    fi

    # Copy the file
    cp "$source_file" "$target_file"
    chmod +x "$target_file"

    log_success "Installed: $target_file"
}

# Uninstall bg-auth.sh
uninstall_bg_auth() {
    local target_file="$INSTALL_DIR/bg-auth.sh"

    log_info "Uninstalling bg-auth.sh..."

    if [[ ! -f "$target_file" ]]; then
        log_warn "bg-auth.sh not found at $target_file"
        log_info "Nothing to uninstall"
        exit 0
    fi

    rm -f "$target_file"
    log_success "Removed: $target_file"
}

# Check if install directory is in PATH
check_path() {
    local path_updated=false

    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        log_warn "$INSTALL_DIR is not in your PATH"
        echo ""

        # Determine which shell config to update
        local shell_config=""

        if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
            shell_config="$HOME/.zshrc"
        elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
            if [[ -f "$HOME/.bash_profile" ]]; then
                shell_config="$HOME/.bash_profile"
            else
                shell_config="$HOME/.bashrc"
            fi
        fi

        if [[ -n "$shell_config" ]]; then
            log_info "Add $INSTALL_DIR to your PATH by adding this line to $shell_config:"
            echo ""
            echo "    export PATH=\"\$PATH:$INSTALL_DIR\""
            echo ""

            if [[ "$FORCE" == "true" ]]; then
                # Auto-add to shell config
                {
                    echo ""
                    echo "# Added by Bedrock Gateway installer"
                    echo "export PATH=\"\$PATH:$INSTALL_DIR\""
                } >> "$shell_config"
                log_success "Added to $shell_config"
                path_updated=true
            else
                read -r -p "Add this line automatically? [y/N] " response
                if [[ "$response" =~ ^[Yy]$ ]]; then
                    {
                        echo ""
                        echo "# Added by Bedrock Gateway installer"
                        echo "export PATH=\"\$PATH:$INSTALL_DIR\""
                    } >> "$shell_config"
                    log_success "Added to $shell_config"
                    path_updated=true
                fi
            fi
        else
            log_info "Add this to your shell configuration:"
            echo "    export PATH=\"\$PATH:$INSTALL_DIR\""
        fi

        if [[ "$path_updated" == "true" ]]; then
            echo ""
            log_info "Run this to apply the change:"
            echo "    source $shell_config"
        fi
    else
        log_success "$INSTALL_DIR is already in PATH"
    fi
}

# Print post-installation instructions
print_next_steps() {
    echo ""
    echo "=============================================="
    echo "  Installation Complete!"
    echo "=============================================="
    echo ""
    echo "Next steps:"
    echo ""
    echo "1. Configure your gateway URL:"
    echo "   export BG_GATEWAY_URL=\"https://your-gateway.example.com\""
    echo ""
    echo "2. Ensure you're logged in to AWS:"
    echo "   aws sso login --profile your-profile"
    echo ""
    echo "3. Test the authentication:"
    echo "   bg-auth.sh --profile your-profile"
    echo ""
    echo "4. Configure Claude Code (copy cli/claude-settings.example.json):"
    echo "   cp cli/claude-settings.example.json ~/.claude/settings.json"
    echo "   # Edit the file and set your gateway URL"
    echo ""
    echo "For M2M (EKS containers), see cli/examples/ for Dockerfile and K8s manifests."
    echo ""
}

# Main function
main() {
    echo ""
    echo "Bedrock Gateway CLI Installer v$VERSION"
    echo "========================================"
    echo ""

    parse_args "$@"

    if [[ "$UNINSTALL" == "true" ]]; then
        uninstall_bg_auth
        log_success "Uninstallation complete"
        exit 0
    fi

    check_dependencies
    echo ""

    install_bg_auth
    echo ""

    check_path
    echo ""

    print_next_steps
}

# Run main if not being sourced
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
