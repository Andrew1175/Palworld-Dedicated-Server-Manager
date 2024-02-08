import subprocess
import time
import os
import re
from datetime import datetime
import psutil
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import webbrowser
import zipfile
import requests
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog
from tkinter import messagebox

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), "Documents/Palworld Server Manager", "settings.json")

def settings_directory():
    settings_directory = os.path.join(os.path.expanduser("~"), "Documents/Palworld Server Manager")
    if not os.path.exists(settings_directory):
        os.makedirs(settings_directory)

def save_settings():
    settings_directory()
    settings = {
        "restartEntry": restartEntry.get(),
        "monitorEntry": monitorEntry.get(),
        "server_directory_selection": server_directory_selection.cget("text"),
        "arrcon_directory_selection": arrcon_directory_selection.cget("text"),
        "steamcmd_directory_selection": steamcmd_directory_selection.cget("text"),
        "backup_directory_selection": backup_directory_selection.cget("text"),
        "server_start_args_entry": server_start_args_entry.get(),
        "email_address_entry": email_address_entry.get(),
        "discordEntry": discordEntry.get(),
        "smtp_server_entry": smtp_server_entry.get(),
        "smtp_port_entry": smtp_port_entry.get()
    }
    with open(SETTINGS_FILE, "w") as file:
        json.dump(settings, file)

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as file:
            settings = json.load(file)
            restartEntry.insert(0, settings.get("restartEntry", ""))
            monitorEntry.insert(0, settings.get("monitorEntry", ""))
            server_directory_selection.config(text=settings.get("server_directory_selection", "No directory selected"))
            arrcon_directory_selection.config(text=settings.get("arrcon_directory_selection", "No directory selected"))
            steamcmd_directory_selection.config(text=settings.get("steamcmd_directory_selection", "No directory selected"))
            backup_directory_selection.config(text=settings.get("backup_directory_selection", "No directory selected"))
            server_start_args_entry.insert(0, settings.get("server_start_args_entry", '-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS -EpicApp=PalServer'))
            email_address_entry.insert(0, settings.get("email_address_entry", ""))
            discordEntry.insert(0, settings.get("discordEntry", ""))
            smtp_server_entry.insert(0, settings.get("smtp_server_entry", "smtp.gmail.com"))
            smtp_port_entry.insert(0, settings.get("smtp_port_entry", "587"))
    except FileNotFoundError:
        append_to_output("First time startup. Applying default configuration")
        server_directory_selection.config(text="No directory selected", foreground="red")
        arrcon_directory_selection.config(text="No directory selected", foreground="red")
        steamcmd_directory_selection.config(text="No directory selected", foreground="red")
        backup_directory_selection.config(text="No directory selected", foreground="red")
        server_start_args_entry.insert(0, '-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS -EpicApp=PalServer')
        smtp_server_entry.insert(0, "smtp.gmail.com")
        smtp_port_entry.insert(0, "587")

#Save settings when exiting app
def on_exit():
    save_settings()
    root.destroy()

#commands used to save palworld server.
arrcon_command_save_server = None
arrcon_command_info_server = None
arrcon_command_shutdown_server = None
arrcon_command_server_message_30 = None
arrcon_command_server_message_10 = None
update_palworld_server_command = None
update_palworld_then_start_server_command = None
shutdown_server_command = None
force_shutdown_server_command = None

#define variables used in functions
send_email_checked = False
discord_message_checked = False
update_server_on_startup = False
enable_backups = False
start_server_clicked = False
after_id = None
monitor_after_id = None
current_function = None
scheduled_time = None

def update_commands():
    global arrcon_command_save_server, arrcon_command_shutdown_server, arrcon_command_server_message_30, arrcon_command_server_message_10, start_server_command, shutdown_server_command, rcon_pass, force_shutdown_server_command, arrcon_command_info_server
    try:
        arrcon_exe_path = f'{arrcon_directory_selection.cget("text")}/ARRCON.exe'
        rcon_getport = rcon_port.cget("text")
        palworld_directory = server_directory_selection.cget("text")
        server_start_args = server_start_args_entry.get()
        arrcon_command_save_server = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "save"'
        arrcon_command_info_server = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "info"'
        arrcon_command_shutdown_server = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "shutdown 60 The_server_will_be_restarting_in_60_seconds"'
        arrcon_command_server_message_30 = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "broadcast The_server_will_be_restarting_in_30_seconds"'
        arrcon_command_server_message_10 = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "broadcast The_server_will_be_restarting_in_10_seconds"'
        start_server_command = f'{palworld_directory}/PalServer.exe {server_start_args}'
        shutdown_server_command = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "shutdown 5 The_server_will_be_shutting_down_in_5_seconds"'
        force_shutdown_server_command = f'{arrcon_exe_path} -H 127.0.0.1 -P {rcon_getport} -p {rcon_pass} "doexit"'
        return "commands updated"
    except Exception as e:
        append_to_output(f"There was an issue creating the ARRCON commands and server startup command. Error: " + str(e))

# Function that sends message to output window
def append_to_output(message):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    formatted_message = timestamp + message
    output_text.insert(tk.END, formatted_message + "\n")
    output_text.yview(tk.END)  # Auto-scroll to the bottom

def server_status_info():
    task_name = "PalServer-Win64-Test-Cmd.exe"

    running_processes = [proc.name() for proc in psutil.process_iter()]
    if task_name in running_processes:
        results = server_check_update_commands()
        if results == "good":
            try:
                process = subprocess.Popen(arrcon_command_info_server, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

                stdout = process.communicate()

                if process.returncode == 0:
                    if isinstance(stdout, tuple):
                        stdout = stdout[0]

                        # Check if stdout is not None and is a string before splitting
                        if stdout and isinstance(stdout, str):
                            output_lines = stdout.splitlines()

                            # Extract the server version using a regular expression
                            version_pattern = re.compile(r'Welcome to Pal Server\[v([\d.]+)\]')
                            match = version_pattern.search(output_lines[1])

                            if match:
                                server_version = match.group(1)
                                server_version_state_label.config(text=server_version)
                                server_status_state_label.config(text="Online", foreground="green")
                                root.after(60000, server_status_info)
                            else:
                                server_version_state_label.config(text="?")
                                server_status_state_label.config(text="Online", foreground="green")
                                root.after(60000, server_status_info)
                        else:
                            server_version_state_label.config(text="?")
                            server_status_state_label.config(text="Online", foreground="green")
                            root.after(60000, server_status_info)
                else:
                    server_version_state_label.config(text="?")
                    server_status_state_label.config(text="Offline", foreground="red")
                    root.after(60000, server_status_info)
            except subprocess.CalledProcessError as e:
                server_status_state_label.config(text="Offline", foreground="red")
                server_version_state_label.config(text="?")
                append_to_output("Unable to update server info due to error: "+ str(e))
                root.after(60000, server_status_info)
        else:
            root.after(60000, server_status_info)
    else:
        server_status_state_label.config(text="Offline", foreground="red")
        server_version_state_label.config(text="?")
        root.after(60000, server_status_info)

def save_server():
    append_to_output("Saving Palworld Server...")
    root.update()
    try:
        subprocess.Popen(arrcon_command_save_server)
        append_to_output("Palworld server was saved successfully...")
    except Exception as e:
        append_to_output(f"Couldn't save the server due to error: " + str(e))

def shutdown_server(type):
    if type == "graceful":
        try:
            subprocess.Popen(shutdown_server_command)
        except Exception as e:
            append_to_output(f"Couldn't shutdown the server due to error: " + str(e))
    if type == "force":
        try:
            subprocess.Popen(force_shutdown_server_command)
        except Exception as e:
            append_to_output(f"Couldn't shutdown the server due to error: " + str(e))

# Function to save the server during the restart interval
def save_server_interval(restartinterval):
    global after_id, current_function, scheduled_time
    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        current_function = "save_server_interval"
        append_to_output("Saving Palworld Server...")
        try:
            subprocess.Popen(arrcon_command_save_server)
        except Exception as e:
            append_to_output(f"Couldn't save the server due to error: " + str(e))
        append_to_output("Palworld server was saved successfully...")
        scheduled_time = time.time() + 5  # Store the scheduled time (5 seconds in the future)
        after_id = root.after(5000, lambda: shutdown_server_interval(restartinterval))
    else:
        current_function = "save_server_interval"
        scheduled_time = time.time() + restartinterval
        trueRestartTime = int(restartinterval / 1000 / 60 / 60)
        append_to_output(f"The Restart interval attempted to run, but the server is not running. This will automatically retry in {trueRestartTime} hour(s)")
        root.after(restartinterval, lambda: save_server_interval(restartinterval))

# Function to shutdown the server
def shutdown_server_interval(restartinterval):
    global after_id, current_function, scheduled_time
    current_function = "shutdown_server"
    append_to_output("Shutting Down Palworld Server...")
    try:
        subprocess.Popen(arrcon_command_shutdown_server)
    except Exception as e:
        append_to_output(f"Couldn't shutdown the server due to error: " + str(e))
    append_to_output("The server will go down in 60 seconds...")
    scheduled_time = time.time() + 70  # Store the scheduled time (30 seconds in the future)
    after_id = root.after(30000, lambda: message_server_30(restartinterval))

# Function to message the server
def message_server_30(restartinterval):
    global after_id, current_function, scheduled_time
    current_function = "message_server_30"
    subprocess.Popen(arrcon_command_server_message_30)
    after_id = root.after(20000, lambda: message_server_10(restartinterval))

# Function to message the server
def message_server_10(restartinterval):
    global after_id, current_function, scheduled_time
    current_function = "message_server_10"
    try:
        subprocess.Popen(arrcon_command_server_message_10)
    except Exception as e:
        append_to_output(f"Couldn't send message to the server due to error: " + str(e))
    after_id = root.after(20000, lambda: restart_server(restartinterval))

# Function to restart the server
def restart_server(restartinterval):
    global after_id, current_function, scheduled_time
    current_function = "restart_server"
    append_to_output("Palworld Server is shutdown. Checking for residual processes... Sometimes the server process gets stuck")
    root.update()
    if enable_backups == True:
            backup_server()

    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        append_to_output(f"Task {task_name} is still running. Ending the process...")
    
        # Find the process by name and terminate it
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] == task_name:
                psutil.Process(proc.info['pid']).terminate()
        root.update()
        time.sleep(3)
        append_to_output("Process ended. Starting the server back up...")
        root.update()
        if update_server_startup_checkbox_var.get():
            results = update_palworld_server()
            if results == "server updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                root.after(3000, check_palworld_process)
                scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
                after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
                trueRestartTime = int(restartinterval / 1000 / 60 / 60)
                append_to_output(f"The server will restart again in {trueRestartTime} hours")
                current_function = None
            elif results == "server not updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                root.after(3000, check_palworld_process)
                scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
                after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
                trueRestartTime = int(restartinterval / 1000 / 60 / 60)
                append_to_output(f"The server will restart again in {trueRestartTime} hours")
                current_function = None
        else:
            append_to_output("Starting server...")
            try:
                subprocess.Popen(start_server_command)
            except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
            root.after(3000, check_palworld_process)
            scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
            after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
            trueRestartTime = int(restartinterval / 1000 / 60 / 60)
            append_to_output(f"The server will restart again in {trueRestartTime} hours")
            current_function = None
    else:
        append_to_output(f"Task {task_name} is not running. Starting the server back up...")
        if update_server_startup_checkbox_var.get():
            results = update_palworld_server()
            if results == "server updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                root.after(3000, check_palworld_process)
                scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
                after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
                trueRestartTime = int(restartinterval / 1000 / 60 / 60)
                append_to_output(f"The server will restart again in {trueRestartTime} hours")
                current_function = None
            elif results == "server not updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                root.after(3000, check_palworld_process)
                scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
                after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
                trueRestartTime = int(restartinterval / 1000 / 60 / 60)
                append_to_output(f"The server will restart again in {trueRestartTime} hours")
                current_function = None
        else:
            append_to_output("Starting server...")
            try:
                subprocess.Popen(start_server_command)
            except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
            root.after(3000, check_palworld_process)
            scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
            after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
            trueRestartTime = int(restartinterval / 1000 / 60 / 60)
            append_to_output(f"The server will restart again in {trueRestartTime} hours")
            current_function = None

def kill_palworld_process():
    append_to_output("Palworld Server is shutdown. Checking for residual processes... Sometimes the server process gets stuck")
    root.update()

    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        append_to_output(f"Task {task_name} is still running. Ending the process...")
        root.update()
    
        # Find the process by name and terminate it
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] == task_name:
                psutil.Process(proc.info['pid']).terminate()
                append_to_output("PalServer.exe is no longer running...")
    else:
        append_to_output("PalServer.exe is not running. The server is completely shutdown")

def check_palworld_process():
    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        append_to_output("Server is now running")

def send_email():
    email_from = email_address_entry.get()
    email_to = email_address_entry.get()
    subject = "Palworld Server Crash"
    body = "This email indicates that the Palworld server was not running. No worries though. The server was restarted. Beep beep boop."

    smtp_server = smtp_server_entry.get()
    smtp_port = smtp_port_entry.get()
    smtp_user = email_address_entry.get()
    smtp_password = email_password_entry.get()

    # Create the MIME object
    msg = MIMEMultipart()
    msg['From'] = email_from
    msg['To'] = email_to
    msg['Subject'] = subject

    # Attach the body of the email
    msg.attach(MIMEText(body, 'plain'))

    try:
        # Connect to the SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()

        # Login to the email account
        server.login(smtp_user, smtp_password)

        # Send the email
        server.sendmail(email_from, email_to, msg.as_string())

        append_to_output("Sent notification email successfully.")

    except Exception as e:
        append_to_output(f"Notification email was not sent successfully due to error: " + str(e))
        send_email_checkbox_var.set(False)

    finally:
        # Disconnect from the SMTP server
        server.quit()

def send_discord_message():
    webhook_url = discordEntry.get()
    message = 'This message indicates that the Palworld server was not running. No worries though, the server was restarted and is back online. Beep beep boop.'
    payload = {"content": message}

    try:
        response = requests.post(webhook_url, json=payload)
        response.raise_for_status()  # Check for HTTP errors

        append_to_output("Discord alert was sent")
    except requests.exceptions.RequestException as e:
        append_to_output(f"Error sending Discord alert: {e}")

def monitor_server(monitorinterval):
    global monitor_after_id
    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))
    elif current_function == "shutdown_server" or current_function == "message_server_30" or current_function == "message_server_10" or current_function == "restart_server":
        monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))
    else:
        if update_server_startup_checkbox_var.get():
            results = update_palworld_server()
            if results == "server updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                if send_email_checked == True:
                    send_email()
                if discord_message_checked == True:
                    send_discord_message()
                monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))
            elif results == "server not updated":
                append_to_output("Starting server...")
                try:
                    subprocess.Popen(start_server_command)
                except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
                if send_email_checked == True:
                    send_email()
                if discord_message_checked == True:
                    send_discord_message()
                monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))
        else:
            append_to_output("Starting server...")
            try:
                subprocess.Popen(start_server_command)
            except Exception as e:
                    append_to_output(f"Couldn't start the server due to error: " + str(e))
            if send_email_checked == True:
                send_email()
            if discord_message_checked == True:
                send_discord_message()
            monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))

# Function that monitors the server process and restarts it if it's not running
def enable_monitor_server():
    global monitor_after_id
    server_check_results = server_check()
    if server_check_results == "check good":
        update_commands_results = update_commands()
        if update_commands_results == "commands updated":
            try:
                if monitor_interval_checkbox_var.get():
                    monitorinterval = int(monitorEntry.get()) * 60 * 1000  # Convert to minutes and then translate to milliseconds
                    true_value = int(monitorinterval / 1000 / 60)
                    append_to_output(f"Monitor Interval has been enabled. The monitor will check every {true_value} minute(s) to ensure the server is running.")
                    monitor_after_id = root.after(monitorinterval, lambda: monitor_server(monitorinterval))
                else:
                    disable_monitor_server()
            except ValueError:
                append_to_output("Your monitor interval cannot be empty and can only contain numerical values")
                monitor_interval_checkbox_var.set(False)
    else:
        append_to_output("Server check failed. Check the Server Config tab and be sure everything is configured first.")
        monitor_interval_checkbox_var.set(False)
    
def disable_monitor_server():
    global monitor_after_id
    try:
        if monitor_interval_checkbox_var.get() == False:
            if monitor_after_id:
                root.after_cancel(monitor_after_id)
                monitor_after_id = None  # Reset id to None
                append_to_output("Monitor interval was disabled.")
    except Exception as e:
        append_to_output("There was an error disabling the monitor interval due to error: " + str(e))

def enable_server_restart():
    global after_id, current_function, scheduled_time
    server_check_results = server_check()
    if server_check_results == "check good":
        update_commands_results = update_commands()
        if update_commands_results == "commands updated":
            try:
                if restart_interval_checkbox_var.get():
                    restartinterval = int(restartEntry.get()) * 60 * 60 * 1000  # Convert to minutes then hours and then translate to milliseconds
                    true_value = int(restartinterval / 1000 / 60 / 60)
                    append_to_output(f"Server Restart Interval has been enabled. The server will restart in {true_value} hour(s)")
                    current_function = "enable_server_restart"
                    scheduled_time = time.time() + restartinterval  # Store the scheduled time (restartinterval seconds in the future)
                    after_id = root.after(restartinterval, lambda: save_server_interval(restartinterval))
                else:
                    disable_server_restart()
            except ValueError:
                append_to_output("Your restart interval cannot be empty and can only contain numerical values")
                restart_interval_checkbox_var.set(False)
    else:
        append_to_output("Server check failed. Check the Server Config tab and be sure everything is configured first.")
        restart_interval_checkbox_var.set(False)

def disable_server_restart():
    global after_id
    try:
        if restart_interval_checkbox_var.get() == False:
            if after_id:
                root.after_cancel(after_id)
                after_id = None  # Reset after_id to None
                append_to_output("Server restart interval stopped.")
    except Exception as e:
        append_to_output("There was an error disabling the server restart interval due to error: " + str(e))

def enable_send_email():
    global send_email_checked
    if send_email_checkbox_var.get():
        if email_address_entry.get() and email_password_entry.get() and smtp_server_entry.get() and smtp_port_entry.get():
            send_email_checked = True
            append_to_output("Email notifications have been enabled")
        else:
            append_to_output("Be sure to fill out all of the information required in the Alerts Config tab")
            messagebox.showinfo("Invalid Email Information", "1 or more fields are missing in the Alerts Config tab")
            send_email_checkbox_var.set(False)
    else:
        disable_send_email()

def disable_send_email():
    global send_email_checked
    if send_email_checkbox_var.get() == False:
        send_email_checked = False
        append_to_output("Email notifications have been disabled")

def enable_send_discord_message():
    global discord_message_checked
    if discordWebhookCheckbox_var.get():
        if discordEntry.get():
            discord_message_checked = True
            append_to_output("Discord alerts have been Enabled")
        else:
            append_to_output("Be sure to enter a Discord Webhook URL in the Alerts Config tab.")
            messagebox.showinfo("Invalid Discord Webhook URL", "You need to enter a Discord Webhook URL in the Alerts Config tab")
            discordWebhookCheckbox_var.set(False)
    elif discordWebhookCheckbox_var.get() == False:
        discord_message_checked = False
        append_to_output("Discord alerts have been Disabled")

def enable_server_updates_on_startup():
    global update_server_on_startup
    if update_server_startup_checkbox_var.get():
        if palworld_exe_result_label.cget("text") == "PalServer.exe detected":
            if steamcmd_exe_result_label.cget("text") == "steamcmd.exe detected":
                update_commands()
                update_server_on_startup = True
                append_to_output("Check for updates on startup has been Enabled")
            else:
                append_to_output("You must select a valid Steamcmd Directory to use this function. Check your Server Config tab")
                messagebox.showinfo("Invalid Directory", "You must select a valid Steamcmd directory to use this function")
                update_server_startup_checkbox_var.set(False)
        else:
            append_to_output("You must select a valid Palworld Server Directory to use this function. Check your Server Config tab")
            messagebox.showinfo("Invalid Directory", "You must select a valid Palworld Server directory to use this function")
            update_server_startup_checkbox_var.set(False)
    elif update_server_startup_checkbox_var.get() == False:
        update_server_on_startup = False
        append_to_output("Check for updates on startup has been Disabled")

def enable_server_backups():
    global enable_backups
    if backup_server_checkbox_var.get():
        if not palworld_exe_result_label.cget("text") == "PalServer.exe not found":
            if not backup_directory_selection.cget("text") == "No directory selected":
                enable_backups = True
                append_to_output("Server backups have been Enabled")
            else:
                append_to_output("You need to select a directory where your backups will reside. Check the Server Config tab")
                messagebox.showinfo("Invalid Directory", "You have not selected a backup directory. Check the Server Config tab")
                backup_server_checkbox_var.set(False)
        else:
            append_to_output("PalServer.exe was not detected in your selected directory. Check the Server Config tab")
            messagebox.showinfo("Invalid Directory", "PalServer.exe was not found in the selected directory. Check the Server Config tab")
            backup_server_checkbox_var.set(False)
    elif backup_server_checkbox_var.get() == False:
        enable_backups = False
        append_to_output("Server backups have been Disabled")

def backup_server():
    if not palworld_exe_result_label.cget("text") == "PalServer.exe not found":
        if not backup_directory_selection.cget("text") == "No directory selected":
            palworld_directory = server_directory_selection.cget("text")
            backup_dir = backup_directory_selection.cget("text")
            source_dir = f"{palworld_directory}/Pal/Saved/SaveGames/0"

            # Create the backup directory if it doesn't exist
            os.makedirs(backup_dir, exist_ok=True)

            # Get the current date and time
            current_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")

            # Compose the backup file path
            backup_file_path = os.path.join(backup_dir, f"palworld_backup_{current_datetime}.zip")

            files_to_backup = []
            for root, dirs, files in os.walk(source_dir):
                files_to_backup.extend(os.path.join(root, file) for file in files)

            if files_to_backup:
                with zipfile.ZipFile(backup_file_path, 'w', zipfile.ZIP_DEFLATED) as zip_archive:
                    for root, dirs, files in os.walk(source_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, source_dir)
                            zip_archive.write(file_path, arcname=arcname)

            # Print a message indicating the completion of the backup
            append_to_output(f"Backup of {source_dir} completed at {backup_file_path}")
        else:
            append_to_output("You must select a Backup Directory to use this function. Check your Server Config tab")
            messagebox.showinfo("Invalid Directory", "You must select a valid Backup directory to use this function")
    else:
        append_to_output("You must select a valid Palworld Server Directory to use this function. Check your Server Config tab")
        messagebox.showinfo("Invalid Directory", "You must select a valid Palworld Server directory to use this function")

# Function that the start server button triggers
def start_server():
    server_check_results = server_check()
    if server_check_results == "check good":
        update_commands_results = update_commands()
        if update_commands_results == "commands updated":
            task_name = "PalServer-Win64-Test-Cmd.exe"

            # Get the list of running processes
            running_processes = [proc.name() for proc in psutil.process_iter()]

            # Check if the process is in the list
            if task_name in running_processes:
                append_to_output("Server is already running... nothing to do")
            else:
                if update_server_startup_checkbox_var.get():
                    results = update_palworld_server()
                    if results == "server updated":
                        append_to_output("Starting server...")
                        try:
                            subprocess.Popen(start_server_command)
                        except Exception as e:
                            append_to_output("There was an issue starting the server due to error: " + str(e))
                        root.after(3000, check_palworld_process)
                    elif results == "server not updated":
                        append_to_output("Starting server...")
                        try:
                            subprocess.Popen(start_server_command)
                        except Exception as e:
                            append_to_output("There was an issue starting the server due to error: " + str(e))
                        root.after(3000, check_palworld_process)
                else:
                    append_to_output("Starting server...")
                    try:
                        subprocess.Popen(start_server_command)
                    except Exception as e:
                        append_to_output("There was an issue starting the server due to error: " + str(e))
                    root.after(3000, check_palworld_process)
        else:
            append_to_output("ARRCON commands failed to update. Check the Server Config tab and be sure everything is configured first.")
    else:
        append_to_output("Server check failed. Check the Server Config tab and be sure everything is configured first.")

def graceful_shutdown():
    global current_function, scheduled_time
    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        if current_function == "shutdown_server" or current_function == "message_server_30" or current_function == "message_server_10":
            time_left = int(max(0, scheduled_time - time.time()))
            append_to_output(f"The server is already in the process of shutting down. Shutdown will complete in: {time_left} seconds.")
        else:
            results = update_commands()
            if results == "commands updated":
                save_server()
                append_to_output("The server will shutdown in 10 seconds...")
                root.update()
                root.after(5000, shutdown_server("graceful"))
                root.after(13000, kill_palworld_process)
            else:
                append_to_output("RCON server shutdown commands did not update. Please check the Server Config tab.")
    else:
        append_to_output("Palworld server is not running. Nothing to stop.")

def force_shutdown():
    global current_function, scheduled_time
    task_name = "PalServer-Win64-Test-Cmd.exe"

    # Get the list of running processes
    running_processes = [proc.name() for proc in psutil.process_iter()]

    # Check if the process is in the list
    if task_name in running_processes:
        if current_function == "shutdown_server" or current_function == "message_server_30" or current_function == "message_server_10":
            time_left = int(max(0, scheduled_time - time.time()))
            append_to_output(f"The server is already in the process of shutting down. Shutdown will complete in: {time_left} seconds.")
        else:
            results = update_commands()
            if results == "commands updated":
                save_server()
                append_to_output("Performing a forceful shutdown...")
                root.after(1000, shutdown_server("force"))
                root.after(2000, kill_palworld_process)
            else:
                append_to_output("RCON server shutdown commands did not update. Please check the Server Config tab.")
    else:
        append_to_output("Palworld server is not running. Nothing to stop.")

def update_palworld_server():
    if palworld_exe_result_label.cget("text") == "PalServer.exe detected":
        if steamcmd_exe_result_label.cget("text") == "steamcmd.exe detected":
            task_name = "PalServer-Win64-Test-Cmd.exe"
            running_processes = [proc.name() for proc in psutil.process_iter()]
            if task_name not in running_processes:
                palworld_directory = server_directory_selection.cget("text")
                steamcmd_directory = steamcmd_directory_selection.cget("text")
                update_palworld_command = f'call {steamcmd_directory}/steamcmd.exe +force_install_dir {palworld_directory} +login anonymous +app_update 2394010 +quit'
                try:
                    process = subprocess.Popen(update_palworld_command, shell=True, stdout=subprocess.PIPE, text=True, universal_newlines=True)
                    for line in process.stdout:
                        append_to_output(line.strip())
                        root.update()
                    return_code = process.wait()
                    if return_code == 0:
                        append_to_output("The server has updated successfully")
                        return "server updated"
                    else:
                        append_to_output("The server was unable to check for updates")
                        return "server not updated"
                except Exception as e:
                    append_to_output(f"Couldn't update the server due to error: " + str(e))
            else:
                append_to_output("Server is running. Cannot update the server unless the server is stopped.")
                messagebox.showinfo("Server Running", "Cannot run this function unless the server is stopped")
        else:
            append_to_output("You must select a valid Steamcmd Directory to use this function. Check your Server Config tab")
            messagebox.showinfo("Invalid Directory", "You must select a valid Steamcmd directory to use this function")
    else:
        append_to_output("You must select a valid Palworld Server Directory to use this function. Check your Server Config tab")
        messagebox.showinfo("Invalid Directory", "You must select a valid Palworld Server directory to use this function")

def validate_palworld_server():
    if palworld_exe_result_label.cget("text") == "PalServer.exe detected":
        if steamcmd_exe_result_label.cget("text") == "steamcmd.exe detected":
            task_name = "PalServer-Win64-Test-Cmd.exe"
            running_processes = [proc.name() for proc in psutil.process_iter()]
            if task_name not in running_processes:
                palworld_directory = server_directory_selection.cget("text")
                steamcmd_directory = steamcmd_directory_selection.cget("text")
                validate_palworld_command = f'call {steamcmd_directory}/steamcmd.exe +force_install_dir {palworld_directory} +login anonymous +app_update 2394010 validate +quit'
                try:
                    process = subprocess.Popen(validate_palworld_command, shell=True, stdout=subprocess.PIPE, text=True, universal_newlines=True)
                    for line in process.stdout:
                        append_to_output(line.strip())
                        root.update()
                    return_code = process.wait()
                    if return_code == 0:
                        append_to_output("The server was validated successfully")
                except Exception as e:
                    append_to_output(f"Couldn't validate the server due to error: " + str(e))
            else:
                append_to_output("Server is running. Cannot update the server unless the server is stopped.")
                messagebox.showinfo("Server Running", "Cannot run this function unless the server is stopped")
        else:
            append_to_output("You must select a valid Steamcmd Directory to use this function. Check your Server Config tab")
            messagebox.showinfo("Invalid Directory", "You must select a valid Steamcmd directory to use this function")
    else:
        append_to_output("You must select a valid Palworld Server Directory to use this function. Check your Server Config tab")
        messagebox.showinfo("Invalid Directory", "You must select a valid Palworld Server directory to use this function")

def server_check_update_commands():
    server_check_results = server_check()
    if server_check_results == "check good":
        update_commands_results = update_commands()
        if update_commands_results == "commands updated":
            return "good"

def server_check():
    if palworld_exe_result_label.cget("text") == "PalServer.exe detected":
        if arrcon_exe_result_label.cget("text") == "ARRCON.exe detected":
            if isinstance(rcon_port.cget("text"), int):
                if rcon_state.cget("text") == "True":
                    return "check good"
                else:
                    append_to_output('RCON is disabled. Be sure the flag "RCONEnabled=True" is set in your PalWorldSettings.ini file. Check the Server Config tab')
                    messagebox.showinfo("RCON Disabled", "RCON needs to be enabled")
            else:
                append_to_output("No RCON port was detected. Check your PalWorldSettings.ini file and the Server Config tab")
                messagebox.showinfo("RCON Port Issue", "No RCON port was detected")
        else:
            append_to_output("ARRCON.exe was not detected in your selected directory. Check the Server Config tab")
            messagebox.showinfo("Invalid Directory", "ARRCON.exe was not found in the selected directory")
    else:
        append_to_output("PalServer.exe was not detected in your selected directory. Check the Server Config tab")
        messagebox.showinfo("Invalid Directory", "PalServer.exe was not found in the selected directory")

def select_palworld_directory():
    directory_path = filedialog.askdirectory()
    if directory_path:
        server_directory_selection.config(text=f"{directory_path}", foreground="black")
        search_file(directory_path, "PalServer.exe")
        get_server_info(directory_path)
    else:
        server_directory_selection.config(text="No directory selected", foreground="red")
        palworld_exe_result_label.config(text="PalServer.exe not found", foreground="red")
        append_to_output("The directory you selected does not contain the PalServer.exe and other file information required to run this application. Please verify the directory")
        messagebox.showinfo("Invalid Directory", "PalServer.exe was not found in the selected directory")

def select_arrcon_directory():
    directory_path = filedialog.askdirectory()
    if directory_path:
        arrcon_directory_selection.config(text=f"{directory_path}", foreground="black")
        search_file(directory_path, "ARRCON.exe")
    else:
        arrcon_directory_selection.config(text="No directory selected", foreground="red")
        arrcon_exe_result_label.config(text="ARRCON.exe not found", foreground="red")
        append_to_output("The directory you selected does not contain the ARRCON.exe required to run this application. Please verify the directory")
        messagebox.showinfo("Invalid Directory", "ARRCON.exe was not found in the selected directory")

def select_steamcmd_directory():
    directory_path = filedialog.askdirectory()
    if directory_path:
        steamcmd_directory_selection.config(text=f"{directory_path}", foreground="black")
        search_file(directory_path, "steamcmd.exe")
    else:
        steamcmd_directory_selection.config(text="No directory selected", foreground="red")
        steamcmd_exe_result_label.config(text="steamcmd.exe not found", foreground="red")
        append_to_output("The directory you selected does not contain the steamcmd.exe. Please verify the directory")
        messagebox.showinfo("Invalid Directory", "steamcmd.exe was not found in the selected directory")

def select_backup_directory():
    directory_path = filedialog.askdirectory()
    if directory_path:
        backup_directory_selection.config(text=f"{directory_path}", foreground="black")
    else:
        backup_directory_selection.config(text="No directory selected", foreground="red")

def search_file(directory, target_file):
    if target_file == "PalServer.exe":
        if not directory == "No directory selected":
            files_in_directory = os.listdir(directory)
            if target_file in files_in_directory:
                palworld_exe_result_label.config(text=f"{target_file} detected", foreground="green")
                return
            else:
                palworld_exe_result_label.config(text=f"{target_file} not found", foreground="red")
                messagebox.showinfo("Invalid Directory", "PalServer.exe was not found in the selected directory")
        else:
            palworld_exe_result_label.config(text=f"{target_file} not found", foreground="red")
    elif target_file == "ARRCON.exe":
        if not directory == "No directory selected":
            files_in_directory = os.listdir(directory)
            if target_file in files_in_directory:
                arrcon_exe_result_label.config(text=f"{target_file} detected", foreground="green")
                return
            else:
                arrcon_exe_result_label.config(text=f"{target_file} not found", foreground="red")
                messagebox.showinfo("Invalid Directory", "ARRCON.exe was not found in the selected directory")
        else:
            arrcon_exe_result_label.config(text=f"{target_file} not found", foreground="red")
    elif target_file == "steamcmd.exe":
        if not directory == "No directory selected":
            files_in_directory = os.listdir(directory)
            if target_file in files_in_directory:
                steamcmd_exe_result_label.config(text=f"{target_file} detected", foreground="green")
                return
            else:
                steamcmd_exe_result_label.config(text=f"{target_file} not found", foreground="red")
                messagebox.showinfo("Invalid Directory", "steamcmd.exe was not found in the selected directory")
        else:
            steamcmd_exe_result_label.config(text=f"{target_file} not found", foreground="red")

def reset_server_info():
    rcon_port.config(text="-")
    rcon_state.config(text="-")
    max_players.config(text="-")
    server_name.config(text="-")
    server_description.config(text="-")
    server_password.config(text="-")
    server_port.config(text="-")

def get_server_info(directory):
    global rcon_pass
    if not directory == "No directory selected":
        file_path = os.path.join(directory, 'Pal', 'Saved', 'Config', 'WindowsServer', 'PalWorldSettings.ini')
        if os.path.isfile(file_path):
            with open(file_path, 'r') as file:
                file_content = file.read()
                max_players_match = re.search(r'ServerPlayerMaxNum=(\d+),', file_content)
                server_name_match = re.search(r'ServerName="([^"]+)",', file_content)
                server_description_match = re.search(r'ServerDescription="([^"]+)",', file_content)
                server_password_match = re.search(r'ServerPassword="([^"]*)",', file_content)
                server_port_match = re.search(r'PublicPort=(\d+),', file_content)
                rcon_port_match = re.search(r'RCONPort=(\d+),', file_content)
                rcon_enable_match = re.search(r'RCONEnabled=(\w+),', file_content)
                rcon_password_match = re.search(r'AdminPassword="([^"]*)",', file_content)
                if rcon_port_match:
                    port = int(rcon_port_match.group(1))
                    rcon_port.config(text=port)
                if rcon_enable_match:
                    state = str(rcon_enable_match.group(1))
                    rcon_state.config(text=state)
                if rcon_password_match:
                    rcon_pass = str(rcon_password_match.group(1))
                    if rcon_pass == "":
                        rcon_password.config(text="No Password Set")
                    else:
                        rcon_password.config(text="********")
                if max_players_match:
                    max = int(max_players_match.group(1))
                    max_players.config(text=max)
                if server_name_match:
                    server = str(server_name_match.group(1))
                    server_name.config(text=server)
                if server_description_match:
                    description = str(server_description_match.group(1))
                    server_description.config(text=description)
                if server_password_match:
                    serv_pass = str(server_password_match.group(1))
                    if serv_pass == "":
                        server_password.config(text="No Password Set")
                    else:
                        server_password.config(text=serv_pass)
                if server_port_match:
                    serv_port = int(server_port_match.group(1))
                    server_port.config(text=serv_port)
        else:
            reset_server_info()
    else:
        reset_server_info()
    
def open_ini_file(directory):
    if not directory == "No directory selected":
        ini_file_path = os.path.join(directory, 'Pal', 'Saved', 'Config', 'WindowsServer', 'PalWorldSettings.ini')
        if os.path.isfile(ini_file_path):
            try:
                subprocess.Popen(['start', '', ini_file_path], shell=True)
            except Exception as e:
                append_to_output("Error opening file: " + str(e))
        else:
            append_to_output("You need to select a valid directory first.")
            messagebox.showinfo("Invalid Directory", "You need to select a valid directory first")
    else:
        append_to_output("You need to select a valid directory first.")
        messagebox.showinfo("Invalid Directory", "You need to select a valid directory first")

def open_discord(event):
    webbrowser.open("https://discord.gg/bPp9kfWe5t")

def open_BMAB(event):
    webbrowser.open("https://www.buymeacoffee.com/thewisestguy")

def check_for_updates():
    webbrowser.open("https://github.com/Andrew1175/Palworld-Dedicated-Server-Manager/releases")

def report_bug():
    webbrowser.open("https://github.com/Andrew1175/Palworld-Dedicated-Server-Manager/issues")

def functions_go_button_click():
    selected_function = functions_combobox.get()
    if selected_function == "Start Server":
        start_server()
    elif selected_function == "Graceful Shutdown":
        graceful_shutdown()
    elif selected_function == "Force Shutdown":
        force_shutdown()
    elif selected_function == "Update Server":
        update_palworld_server()
    elif selected_function == "Validate Server Files":
        validate_palworld_server()
    elif selected_function == "Backup Server":
        backup_server()


############################## Root Code ######################################################
root = tk.Tk()
root.title("Palworld Dedicated Server Manager")
try:
    root.iconbitmap('palworld_logo.ico')
except Exception as e:
    append_to_output("Icon wasn't able to load due to error: " + str(e))

tabControl = ttk.Notebook(root)

mainTab = ttk.Frame(tabControl)
serverTab = ttk.Frame(tabControl)
alertsTab = ttk.Frame(tabControl)
aboutTab = ttk.Frame(tabControl)

tabControl.add(mainTab, text='Main')
tabControl.add(serverTab, text='Server Config')
tabControl.add(alertsTab, text='Alerts Config')
tabControl.add(aboutTab, text='About')
tabControl.pack(expand = 1, fill="both")

mainTab.columnconfigure(0, weight=1)
mainTab.columnconfigure(1, weight=1)
serverTab.columnconfigure(0, weight=1)
alertsTab.columnconfigure(0, weight=1)
alertsTab.columnconfigure(1, weight=1)
aboutTab.columnconfigure(0, weight=1)

###################### Main Tab ################################################################
###################### Interval Configurations ###################################################
interval_frame = tk.LabelFrame(mainTab, text="Interval Configuration")
interval_frame.grid(column=0, row=0, padx=10, pady=10, sticky=tk.NSEW)

restart_interval_checkbox_var = tk.BooleanVar()

restart_interval_checkbox = ttk.Checkbutton(interval_frame, variable=restart_interval_checkbox_var, command=enable_server_restart)
restart_interval_checkbox.grid(column=0, row=0)

restartLabel = ttk.Label(interval_frame, text="Server Restart Interval (hours):")
restartLabel.grid(column=1, row=0, sticky=tk.W)

restartEntry = ttk.Entry(interval_frame, width=5)
restartEntry.grid(column=2, row=0)

monitor_interval_checkbox_var = tk.BooleanVar()

monitor_interval_checkbox = ttk.Checkbutton(interval_frame, variable=monitor_interval_checkbox_var, command=enable_monitor_server)
monitor_interval_checkbox.grid(column=0, row=1)

monitorLabel = ttk.Label(interval_frame, text="Monitor Interval (minutes):")
monitorLabel.grid(column=1, row=1, sticky=tk.W)

monitorEntry = ttk.Entry(interval_frame, width=5)
monitorEntry.grid(column=2, row=1)

send_email_checkbox_var = tk.BooleanVar()

send_email_checkbox = ttk.Checkbutton(interval_frame, variable=send_email_checkbox_var, command=enable_send_email)
send_email_checkbox.grid(column=0, row=2)

send_email_label = ttk.Label(interval_frame, text="Send Notification Email on crash")
send_email_label.grid(column=1, row=2, sticky=tk.W)

discordWebhookCheckbox_var = tk.BooleanVar()

discordWebhookCheckbox = ttk.Checkbutton(interval_frame, variable=discordWebhookCheckbox_var, command=enable_send_discord_message)
discordWebhookCheckbox.grid(column=0, row=3)

discordWebhookLabel = ttk.Label(interval_frame, text="Send Discord channel message on crash")
discordWebhookLabel.grid(column=1, row=3, sticky=tk.W)

###################### Optional Configurations ###################################################

optional_config_frame = tk.LabelFrame(mainTab, text="Optional Configurations")
optional_config_frame.grid(column=0, row=1, padx=10, pady=10, sticky=tk.NSEW)

update_server_startup_checkbox_var = tk.BooleanVar()

update_server_startup_checkbox = ttk.Checkbutton(optional_config_frame, variable=update_server_startup_checkbox_var, command=enable_server_updates_on_startup)
update_server_startup_checkbox.grid(column=0, row=0)

update_server_startup_label = ttk.Label(optional_config_frame, text="Check for updates on startup")
update_server_startup_label.grid(column=1, row=0, sticky=tk.W)

backup_server_checkbox_var = tk.BooleanVar()

backup_server_checkbox = ttk.Checkbutton(optional_config_frame, variable=backup_server_checkbox_var, command=enable_server_backups)
backup_server_checkbox.grid(column=0, row=1)

backup_server_label = ttk.Label(optional_config_frame, text="Backup server during restart")
backup_server_label.grid(column=1, row=1, sticky=tk.W)

###################### Server Functions ###################################################

server_functions_frame = tk.LabelFrame(mainTab, text="Server Functions")
server_functions_frame.grid(column=0, row=2, padx=10, pady=10, sticky=tk.NSEW)

functions_combobox = ttk.Combobox(server_functions_frame, justify="center", state="readonly", values=["Start Server", "Graceful Shutdown", "Force Shutdown", "Update Server", "Validate Server Files", "Backup Server"])
functions_combobox.grid(column=0, row=0, padx=10, pady=10)
functions_combobox.set("-SELECT-")

functions_go_button = ttk.Button(server_functions_frame, text="Run", command=functions_go_button_click)
functions_go_button.grid(column=1, row=0)

###################### Server Information ###################################################

server_info_frame = tk.LabelFrame(mainTab, text="Server Information")
server_info_frame.grid(column=1, row=0, padx=10, pady=10, sticky=tk.NSEW)

server_status_label = ttk.Label(server_info_frame, text="Server Status:")
server_status_label.grid(column=0, row=0, sticky=tk.W)

server_status_state_label = ttk.Label(server_info_frame, text="-")
server_status_state_label.grid(column=1, row=0)

server_version_label = ttk.Label(server_info_frame, text="Server Version:")
server_version_label.grid(column=0, row=1, sticky=tk.W)

server_version_state_label = ttk.Label(server_info_frame, text="-")
server_version_state_label.grid(column=1, row=1)

updateInfoButton = ttk.Button(server_info_frame, text="Update Now", command=server_status_info)
updateInfoButton.grid(column=0, row=3, columnspan=2, sticky=tk.N)

###################### Server Tab ###################################################
###################### PalWorldSetting.ini Frame ###################################################

server_info_frame = tk.LabelFrame(serverTab, text="PalWorldSettings.ini")
server_info_frame.grid(column=0, row=0, padx=10, pady=10)

server_name_label = ttk.Label(server_info_frame, text="Server Name:")
server_name_label.grid(column=0, row=0, sticky=tk.W, padx=10)

server_name = ttk.Label(server_info_frame, text="-")
server_name.grid(column=0, row=1, sticky=tk.W, padx=10)

server_description_label = ttk.Label(server_info_frame, text="Server Description:")
server_description_label.grid(column=0, row=2, sticky=tk.W, padx=10)

server_description = ttk.Label(server_info_frame, text="-")
server_description.grid(column=0, row=3, sticky=tk.W, padx=10)

server_password_label = ttk.Label(server_info_frame, text="Server Password:")
server_password_label.grid(column=0, row=4, sticky=tk.W, padx=10)

server_password = ttk.Label(server_info_frame, text="-")
server_password.grid(column=0, row=5, sticky=tk.W, padx=10)

max_players_label = ttk.Label(server_info_frame, text="Max Players:")
max_players_label.grid(column=1, row=0, sticky=tk.W, padx=10)

max_players = ttk.Label(server_info_frame, text="-")
max_players.grid(column=1, row=1, sticky=tk.W, padx=10)

server_port_label = ttk.Label(server_info_frame, text="Server Port:")
server_port_label.grid(column=1, row=2, sticky=tk.W, padx=10)

server_port = ttk.Label(server_info_frame, text="-")
server_port.grid(column=1, row=3, sticky=tk.W, padx=10)

rcon_port_label = ttk.Label(server_info_frame, text="RCON Port:")
rcon_port_label.grid(column=2, row=0, sticky=tk.W, padx=10)

rcon_port = ttk.Label(server_info_frame, text="-")
rcon_port.grid(column=2, row=1, sticky=tk.W, padx=10)

rcon_state_label = ttk.Label(server_info_frame, text="RCON Enabled:")
rcon_state_label.grid(column=2, row=2, sticky=tk.W, padx=10)

rcon_state = ttk.Label(server_info_frame, text="-")
rcon_state.grid(column=2, row=3, sticky=tk.W, padx=10)

rcon_password_label = ttk.Label(server_info_frame, text="RCON Password:")
rcon_password_label.grid(column=2, row=4, sticky=tk.W, padx=10)

rcon_password = ttk.Label(server_info_frame, text="-")
rcon_password.grid(column=2, row=5, sticky=tk.W, padx=10)

edit_server_config_button = ttk.Button(server_info_frame, text="Edit PalWorldSettings.ini", command=lambda: open_ini_file(server_directory_selection.cget("text")))
edit_server_config_button.grid(column=0, row=6, columnspan=3, padx=10, pady=10)

###################### Server Configuration Frame ###################################################

server_config_frame = tk.LabelFrame(serverTab, text="Server Configuration")
server_config_frame.grid(column=0, row=1, padx=10, pady=10)

server_directory_button = ttk.Button(server_config_frame, text="Select Palworld Directory:", command=select_palworld_directory)
server_directory_button.grid(column=0, row=0, padx=10, pady=10)

server_directory_selection = ttk.Label(server_config_frame, text="No directory selected")
server_directory_selection.grid(column=1, row=0, sticky=tk.W)

palworld_exe_result_label = ttk.Label(server_config_frame)
palworld_exe_result_label.grid(column=2, row=0)

arrcon_directory_button = ttk.Button(server_config_frame, text="Select ARRCON Directory:", command=select_arrcon_directory)
arrcon_directory_button.grid(column=0, row=1, padx=10, pady=10)

arrcon_directory_selection = ttk.Label(server_config_frame, text="No directory selected")
arrcon_directory_selection.grid(column=1, row=1, sticky=tk.W)

arrcon_exe_result_label = ttk.Label(server_config_frame)
arrcon_exe_result_label.grid(column=2, row=1)

steamcmd_directory_button = ttk.Button(server_config_frame, text="Select steamcmd Directory:", command=select_steamcmd_directory)
steamcmd_directory_button.grid(column=0, row=2, padx=10, pady=10)

steamcmd_directory_selection = ttk.Label(server_config_frame, text="No directory selected")
steamcmd_directory_selection.grid(column=1, row=2, sticky=tk.W)

steamcmd_exe_result_label = ttk.Label(server_config_frame)
steamcmd_exe_result_label.grid(column=2, row=2)

backup_directory_button = ttk.Button(server_config_frame, text="Select Backup Directory:", command=select_backup_directory)
backup_directory_button.grid(column=0, row=3, padx=10, pady=10)

backup_directory_selection = ttk.Label(server_config_frame, text="No directory selected")
backup_directory_selection.grid(column=1, row=3, sticky=tk.W)

server_start_args_label = ttk.Label(server_config_frame, text="Server Startup Arguments:")
server_start_args_label.grid(column=0, row=4, padx=10, pady=10)

server_start_args_entry = ttk.Entry(server_config_frame, width=100)
server_start_args_entry.grid(column=1, row=4, columnspan=2, sticky=tk.W)

###################### Alerts Tab ###################################################
###################### Email Configuration Frame ####################################
email_config_frame = tk.LabelFrame(alertsTab, text="Email Configuration")
email_config_frame.grid(column=0, row=0, padx=10, pady=10, sticky=tk.NSEW)

email_address_label = ttk.Label(email_config_frame, text="Email Address:")
email_address_label.grid(column=0, row=0, padx=10, sticky=tk.W)

email_address_entry = ttk.Entry(email_config_frame, width=35)
email_address_entry.grid(column=1, row=0, sticky=tk.W)

email_password_label = ttk.Label(email_config_frame, text="Email Password:")
email_password_label.grid(column=0, row=1, padx=10, sticky=tk.W)

email_password_entry = ttk.Entry(email_config_frame, show="*", width=35)
email_password_entry.grid(column=1, row=1, sticky=tk.W)

smtp_server_label = ttk.Label(email_config_frame, text="SMTP Server:")
smtp_server_label.grid(column=0, row=2, padx=10, sticky=tk.W)

smtp_server_entry = ttk.Entry(email_config_frame)
smtp_server_entry.grid(column=1, row=2, sticky=tk.W)

smtp_port_label = ttk.Label(email_config_frame, text="SMTP Port:")
smtp_port_label.grid(column=0, row=3, padx=10, sticky=tk.W)

smtp_port_entry = ttk.Entry(email_config_frame, width=5)
smtp_port_entry.grid(column=1, row=3, sticky=tk.W)

###################### Discord Configuration Frame ####################################

discord_frame = tk.LabelFrame(alertsTab, text="Discord Configuration")
discord_frame.grid(column=1, row=0, padx=10, pady=10, sticky=tk.NSEW)

discordLabel = ttk.Label(discord_frame, text="Discord Webhook URL:")
discordLabel.grid(column=0, row=0, padx=10)

discordEntry = ttk.Entry(discord_frame, width=35)
discordEntry.grid(column=1, row=0)

###################### About Tab ###################################################

app_info_frame = tk.LabelFrame(aboutTab, text="Application Info")
app_info_frame.grid(column=0, row=0, padx=10, pady=10, sticky=tk.N)

app_version_label = ttk.Label(app_info_frame, text="Application Version: 1.1.2")
app_version_label.grid(column=0, row=0, padx=10)

app_update_button = ttk.Button(app_info_frame, text="Check for Updates", command=check_for_updates)
app_update_button.grid(column=0, row=1, padx=10, pady=10)

report_bug_button = ttk.Button(app_info_frame, text="Report a Bug", command=report_bug)
report_bug_button.grid(column=0, row=2, columnspan=2, padx=10, pady=10)

support_frame = tk.LabelFrame(aboutTab, text="Support Info")
support_frame.grid(column=0, row=1, padx=10, pady=10, sticky=tk.EW)

feedback_label = ttk.Label(support_frame, text="Have feedback or suggestions? Join my discord and let me know:")
feedback_label.grid(column=0, row=0, sticky=tk.E)

feedback_label_link = tk.Label(support_frame, text="https://discord.gg/bPp9kfWe5t", foreground="blue", cursor="hand2")
feedback_label_link.grid(column=1, row=0, sticky=tk.W)
feedback_label_link.bind("<Button-1>", open_discord)

buy_me_beer_label = ttk.Label(support_frame, justify="center", text="This application is completely free and no features will ever be behind a paywall. If you would like to support me I would greatly appreciate it. You can buy me a beer here:")
buy_me_beer_label.grid(column=0, row=1, columnspan=2, sticky=tk.NSEW)

buy_me_beer_link = tk.Label(support_frame, text="https://www.buymeacoffee.com/thewisestguy", foreground="blue", cursor="hand2")
buy_me_beer_link.grid(column=0, row=2, columnspan=2)
buy_me_beer_link.bind("<Button-1>", open_BMAB)



###################### Console Output Frame ###################################################

outputFrame = tk.Frame(root)
outputFrame.pack(side="bottom", expand=True, fill=tk.BOTH)

outputLabel = ttk.Label(outputFrame, text="Output Window:")
outputLabel.pack()

# scrollbar for output window
scrollbar = ttk.Scrollbar(outputFrame, orient='vertical')
scrollbar.pack(side="right", fill="y")

# text widget for the output
output_text = tk.Text(outputFrame, wrap=tk.WORD, height=10, width=85, yscrollcommand=scrollbar.set)
output_text.pack(padx=10, pady=10, expand=True, fill=tk.BOTH)

scrollbar.config(command=output_text.yview)

load_settings()

search_file(server_directory_selection.cget("text"), "PalServer.exe")
search_file(arrcon_directory_selection.cget("text"), "ARRCON.exe")
search_file(steamcmd_directory_selection.cget("text"), "steamcmd.exe")
get_server_info(server_directory_selection.cget("text"))
server_status_info()

root.protocol("WM_DELETE_WINDOW", on_exit)

root.mainloop()
