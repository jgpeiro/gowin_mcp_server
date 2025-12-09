#!/usr/bin/env python3
"""
Gowin MCP Server using FastMCP

Exposes Gowin TCL commands through MCP tools for AI assistants to use.
"""

import subprocess
import threading
import time
from typing import Optional, Dict, Any
from pathlib import Path
from fastmcp import FastMCP

GOWIN_PATH = "C:/Gowin/Gowin_V1.9.12_x64/IDE/bin/gw_sh.exe"

# Initialize FastMCP server
mcp = FastMCP("Gowin TCL Controller")

# Global process state
class GowinProcess:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.output_buffer: list[str] = []
        self.output_lock = threading.Lock()
        self.prompt_ready = threading.Event()
        self.reader_thread: Optional[threading.Thread] = None
        
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None
    
    def read_output(self):
        """Background thread to read process output and detect prompts"""
        try:
            buf = ""
            while self.process and self.process.poll() is None:
                ch = self.process.stdout.read(1)
                if ch == "":
                    if buf:
                        with self.output_lock:
                            self.output_buffer.append(buf)
                        buf = ""
                    break
                buf += ch
                
                if ch == "\n":
                    with self.output_lock:
                        self.output_buffer.append(buf)
                    buf = ""
                    continue
                
                # Detect prompt sequence
                if "% " in buf:
                    idx = buf.index("% ") + 2
                    to_emit = buf[:idx]
                    rest = buf[idx:]
                    with self.output_lock:
                        self.output_buffer.append(to_emit)
                    buf = rest
                    self.prompt_ready.set()
            
            # Leftover
            if buf:
                with self.output_lock:
                    self.output_buffer.append(buf)
        except Exception as e:
            with self.output_lock:
                self.output_buffer.append(f"\n[Reader error: {e}]\n")
    
    def get_output(self) -> str:
        """Get and clear accumulated output"""
        with self.output_lock:
            output = "".join(self.output_buffer)
            self.output_buffer.clear()
            return output
    
    def send_command(self, command: str, wait_for_prompt: bool = True, timeout: float = 30.0) -> str:
        """Send command and optionally wait for prompt, returning output"""
        if not self.is_running():
            raise RuntimeError("Gowin process is not running. Start it first.")
        
        # Clear prompt flag
        self.prompt_ready.clear()
        
        # Get current output (clear buffer)
        self.get_output()
        
        # Send command
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Error sending command: {e}")
        
        # Wait for prompt if requested
        if wait_for_prompt:
            if not self.prompt_ready.wait(timeout=timeout):
                # Timeout - still return what we got
                output = self.get_output()
                return output + "\n[Warning: Command may still be running (timeout)]"
        else:
            # Give it a moment to produce output
            time.sleep(0.5)
        
        return self.get_output()

# Global instance
gowin = GowinProcess()


@mcp.tool()
def start_gowin() -> str:
    """
    Start the Gowin TCL shell process.
    
    Returns:
        Status message
    """
    executable_path = GOWIN_PATH
    use_no_gui = True
    suppress_stderr = False
    
    if gowin.is_running():
        return "Gowin process is already running."
    
    # Build command
    args = [executable_path]
    if use_no_gui:
        args.append("-no_gui")
    
    # Start process
    try:
        stderr_target = subprocess.DEVNULL if suppress_stderr else subprocess.STDOUT
        gowin.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=True,
            bufsize=1
        )
    except Exception as e:
        gowin.process = None
        return f"Error starting process: {e}"
    
    # Start reader thread
    gowin.prompt_ready.clear()
    gowin.reader_thread = threading.Thread(target=gowin.read_output, daemon=True)
    gowin.reader_thread.start()
    
    # Wait for initial prompt
    if not gowin.prompt_ready.wait(timeout=5.0):
        time.sleep(0.5)
    
    # Set interactive mode
    try:
        output = gowin.send_command("set tcl_interactive 1", wait_for_prompt=True, timeout=5.0)
        return f"Gowin process started successfully.\n{output}"
    except Exception as e:
        return f"Gowin process started, but error setting interactive mode: {e}"


@mcp.tool()
def stop_gowin() -> str:
    """
    Stop the Gowin TCL shell process gracefully.
    
    Returns:
        Status message
    """
    if not gowin.is_running():
        return "Gowin process is not running."
    
    try:
        # Try graceful exit
        gowin.send_command("exit", wait_for_prompt=False)
        try:
            gowin.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            gowin.process.terminate()
            try:
                gowin.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                gowin.process.kill()
    except Exception as e:
        try:
            gowin.process.kill()
        except Exception:
            pass
    finally:
        gowin.process = None
        gowin.prompt_ready.clear()
    
    return "Gowin process stopped."


@mcp.tool()
def create_project(
    name: str,
    directory: str,
    part_number: str,
    device_version: str = "C",
    force: bool = True
) -> str:
    """
    Create a new Gowin project.
    
    Args:
        name: Project name
        directory: Project directory path
        part_number: Part number (e.g., "GW2AR-LV18QN88C8/I7")
        device_version: Device version (default: "C")
        force: Overwrite existing project
    
    Returns:
        Command output
    """
    cmd = f'create_project -name {name} -dir "{directory}" -pn {part_number} -device_version {device_version}'
    if force:
        cmd += " -force"
    
    return gowin.send_command(cmd)


@mcp.tool()
def add_file(file_type: str, file_path: str) -> str:
    """
    Add existing file to the current project.
    
    Args:
        file_type: File type (verilog, cst, sdc, vhdl, gao)
        file_path: Path to the file
    
    Returns:
        Command output
    """
    cmd = f'add_file -type {file_type} "{file_path}"'
    return gowin.send_command(cmd)


@mcp.tool()
def set_top_module(module_name: str) -> str:
    """
    Set the top module for the project.
    
    Args:
        module_name: Name of the top module
    
    Returns:
        Command output
    """
    cmd = f'set_option -top_module {module_name}'
    return gowin.send_command(cmd)


@mcp.tool()
def set_output_base_name(base_name: str) -> str:
    """
    Set the output base name for generated files.
    
    Args:
        base_name: Base name for output files
    
    Returns:
        Command output
    """
    cmd = f'set_option -output_base_name {base_name}'
    return gowin.send_command(cmd)


@mcp.tool()
def set_option(option_name: str, option_value: str) -> str:
    """
    Set a generic project option.
    
    Args:
        option_name: Option name (without leading dash)
        option_value: Option value
    
    Returns:
        Command output
    """
    cmd = f'set_option -{option_name} {option_value}'
    return gowin.send_command(cmd)


@mcp.tool()
def run_synthesis() -> str:
    """
    Run synthesis on the current project.
    
    Returns:
        Command output
    """
    return gowin.send_command("run syn", timeout=300.0)


@mcp.tool()
def run_place_and_route() -> str:
    """
    Run place and route on the current project.
    
    Returns:
        Command output
    """
    return gowin.send_command("run pnr", timeout=300.0)


@mcp.tool()
def run_all() -> str:
    """
    Run complete flow (synthesis, place and route) on the current project.
    
    Returns:
        Command output
    """
    return gowin.send_command("run all", timeout=600.0)


@mcp.tool()
def send_tcl_command(command: str, timeout: float = 30.0) -> str:
    """
    Send a custom TCL command to the Gowin shell.
    
    Args:
        command: TCL command to execute
        timeout: Maximum time to wait for command completion (seconds)
    
    Returns:
        Command output
    """
    return gowin.send_command(command, timeout=timeout)


@mcp.tool()
def get_process_status() -> str:
    """
    Check if the Gowin process is running.
    
    Returns:
        Status message
    """
    if gowin.is_running():
        return "Gowin process is running."
    else:
        return "Gowin process is not running."


if __name__ == "__main__":
    # Run the MCP server
    mcp.run(transport="stdio")