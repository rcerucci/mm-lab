#!/usr/bin/env bash
set -e
ROOT="${ROOT:-$HOME/mm-lab}"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/mm-painel" <<SCRIPT
#!/usr/bin/env bash
exec "$ROOT/.venv/bin/python" -u "$ROOT/painel.py" "\$@"
SCRIPT
chmod +x "$HOME/.local/bin/mm-painel"
# ensure non-login ssh finds it: also drop a wrapper in ~/bin if present, and document PATH
# put absolute path note
grep -q 'mm-painel\|.local/bin' "$HOME/.bashrc" 2>/dev/null || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
# For ssh without bashrc: create ~/mm-painel symlink at home too
ln -sfn "$HOME/.local/bin/mm-painel" "$HOME/mm-painel"
echo "installed: $HOME/.local/bin/mm-painel and $HOME/mm-painel"
