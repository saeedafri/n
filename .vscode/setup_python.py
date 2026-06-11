#!/usr/bin/env python3
"""
Setup script to configure VS Code to use the project's .venv
Run this if VS Code is not detecting the correct Python interpreter
"""
import json
import os
import subprocess
import sys

def get_venv_python():
    """Get the path to the venv Python"""
    workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(workspace_root, ".venv", "bin", "python")
    
    if os.path.exists(venv_python):
        return venv_python
    
    # Try alternative paths
    alt_paths = [
        os.path.join(workspace_root, ".venv", "bin", "python3"),
        os.path.join(workspace_root, "venv", "Scripts", "python.exe"),  # Windows
        os.path.join(workspace_root, ".venv", "Scripts", "python.exe"),  # Windows
    ]
    
    for path in alt_paths:
        if os.path.exists(path):
            return path
    
    return None

def main():
    venv_python = get_venv_python()
    
    if not venv_python:
        print("❌ Could not find .venv Python. Make sure virtual environment exists.")
        print(f"   Expected: {os.path.join(os.getcwd(), '.venv', 'bin', 'python')}")
        sys.exit(1)
    
    print(f"✅ Found Python: {venv_python}")
    
    # Test the Python
    try:
        result = subprocess.run(
            [venv_python, "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        print(f"✅ Python version: {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Error running Python: {e}")
        sys.exit(1)
    
    print("\n📝 VS Code Settings:")
    print("   Your workspace is already configured to use:")
    print(f"   ${{workspaceFolder}}/.venv/bin/python")
    print("\n🔄 Next steps:")
    print("   1. Reload VS Code window (Cmd/Ctrl + Shift + P → 'Developer: Reload Window')")
    print("   2. Or restart VS Code")
    print("   3. Check bottom-left corner - it should show the .venv Python")
    
if __name__ == "__main__":
    main()
