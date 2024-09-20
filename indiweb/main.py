#!/usr/bin/env python

import os
import json
import logging
import argparse
import socket
from threading import Timer
import subprocess
import platform
from importlib_metadata import version
from bottle import Bottle, run, template, static_file, request, response, BaseRequest, default_app
from bottle_cors_plugin import cors_plugin
from .indi_server import IndiServer, INDI_PORT, INDI_FIFO, INDI_CONFIG_DIR
from .driver import DeviceDriver, DriverCollection, INDI_DATA_DIR
from .database import Database
from .device import Device
from .indihub_agent import IndiHubAgent

# default settings
WEB_HOST = '0.0.0.0'
WEB_PORT = 8624

# Make it 10MB
BaseRequest.MEMFILE_MAX = 50 * 1024 * 1024

pkg_path, _ = os.path.split(os.path.abspath(__file__))
views_path = os.path.join(pkg_path, 'views')

parser = argparse.ArgumentParser(
    description='INDI Web Manager. '
    'A simple web application to manage an INDI server')

parser.add_argument('--indi-port', '-p', type=int, default=INDI_PORT,
                    help='indiserver port (default: %d)' % INDI_PORT)
parser.add_argument('--port', '-P', type=int, default=WEB_PORT,
                    help='Web server port (default: %d)' % WEB_PORT)
parser.add_argument('--host', '-H', default=WEB_HOST,
                    help='Bind web server to this interface (default: %s)' %
                    WEB_HOST)
parser.add_argument('--fifo', '-f', default=INDI_FIFO,
                    help='indiserver FIFO path (default: %s)' % INDI_FIFO)
parser.add_argument('--conf', '-c', default=INDI_CONFIG_DIR,
                    help='INDI config. directory (default: %s)' % INDI_CONFIG_DIR)
parser.add_argument('--xmldir', '-x', default=INDI_DATA_DIR,
                    help='INDI XML directory (default: %s)' % INDI_DATA_DIR)
parser.add_argument('--verbose', '-v', action='store_true',
                    help='Print more messages')
parser.add_argument('--logfile', '-l', help='log file name')
parser.add_argument('--server', '-s', default='standalone',
                    help='HTTP server [standalone|apache] (default: standalone')
parser.add_argument('--sudo', '-S', action='store_true',                    
                    help='Run poweroff/reboot commands with sudo')

args = parser.parse_args()


logging_level = logging.WARNING

if args.verbose:
    logging_level = logging.DEBUG

if args.logfile:
    logging.basicConfig(filename=args.logfile,
                        format='%(asctime)s - %(levelname)s: %(message)s',
                        level=logging_level)

else:
    logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s',
                        level=logging_level)

logging.debug("command line arguments: " + str(vars(args)))

hostname = socket.gethostname()

collection = DriverCollection(args.xmldir)
indi_server = IndiServer(args.fifo, args.conf)
indi_device = Device()

indihub_agent = IndiHubAgent('%s:%d' % (args.host, args.port), hostname, args.port)

db_path = os.path.join(args.conf, 'profiles.db')
db = Database(db_path)

collection.parse_custom_drivers(db.get_custom_drivers())

if args.server == 'standalone':
    app = Bottle()
    app.install(cors_plugin('*')) # Enable CORS
    logging.info('using Bottle as standalone server')
else:
    app = default_app()
    logging.info('using Apache web server')

saved_profile = None
active_profile = ""


def start_profile(profile):
    info = db.get_profile(profile)

    profile_drivers = db.get_profile_drivers_labels(profile)
    all_drivers = [collection.by_label(d['label']) for d in profile_drivers]

    # Find if we have any remote drivers
    remote_drivers = db.get_profile_remote_drivers(profile)
    if remote_drivers:
        drivers = remote_drivers['drivers'].split(',')
        for drv in drivers:
            logging.warning(f"LOADING REMOTE DRIVER drv is {drv}")
            all_drivers.append(DeviceDriver(drv, drv, "1.0", drv, "Remote"))

    if all_drivers:
        indi_server.start(info['port'], all_drivers)
        # Auto connect drivers in 3 seconds if required.
        if info['autoconnect'] == 1:
            t = Timer(3, indi_server.auto_connect)
            t.start()


@app.route('/static/<path:path>')
def callback(path):
    """Serve static files"""
    return static_file(path, root=views_path)


@app.route('/favicon.ico', method='GET')
def get_favicon():
    """Serve favicon"""
    return static_file('favicon.ico', root=views_path)

@app.route('/<:re:.*>', method='OPTIONS')

@app.route('/')
def main_form():
    """Main page"""
    global saved_profile
    drivers = collection.get_families()

    if not saved_profile:
        saved_profile = request.get_cookie('indiserver_profile') or 'Simulators'

    profiles = db.get_profiles()
    return template(os.path.join(views_path, 'form.tpl'), profiles=profiles,
                    drivers=drivers, saved_profile=saved_profile,
                    hostname=hostname)

###############################################################################
# Profile endpoints
###############################################################################


@app.get('/api/profiles')
def get_json_profiles():
    """Get all profiles (JSON)"""
    results = db.get_profiles()
    return json.dumps(results)


@app.get('/api/profiles/<item>')
def get_json_profile(item):
    """Get one profile info"""
    results = db.get_profile(item)
    return json.dumps(results)


@app.post('/api/profiles/<name>')
def add_profile(name):
    """Add new profile"""
    db.add_profile(name)


@app.delete('/api/profiles/<name>')
def delete_profile(name):
    """Delete Profile"""
    db.delete_profile(name)


@app.put('/api/profiles/<name>')
def update_profile(name):
    """Update profile info (port & autostart & autoconnect)"""
    response.set_cookie("indiserver_profile", name,
                        None, max_age=3600000, path='/')
    data = request.json
    port = data.get('port', args.indi_port)
    autostart = bool(data.get('autostart', 0))
    autoconnect = bool(data.get('autoconnect', 0))
    db.update_profile(name, port, autostart, autoconnect)


@app.post('/api/profiles/<name>/drivers')
def save_profile_drivers(name):
    """Add drivers to existing profile"""
    data = request.json
    db.save_profile_drivers(name, data)


@app.post('/api/profiles/custom')
def save_profile_custom_driver():
    """Add custom driver to existing profile"""
    data = request.json
    db.save_profile_custom_driver(data)
    collection.clear_custom_drivers()
    collection.parse_custom_drivers(db.get_custom_drivers())


@app.get('/api/profiles/<item>/labels')
def get_json_profile_labels(item):
    """Get driver labels of specific profile"""
    results = db.get_profile_drivers_labels(item)
    return json.dumps(results)


@app.get('/api/profiles/<item>/remote')
def get_remote_drivers(item):
    """Get remote drivers of specific profile"""
    results = db.get_profile_remote_drivers(item)
    if results is None:
        results = {}
    return json.dumps(results)


###############################################################################
# Server endpoints
###############################################################################

@app.get('/api/server/status')
def get_server_status():
    """Server status"""
    status = [{'status': str(indi_server.is_running()), 'active_profile': active_profile}]
    return json.dumps(status)


@app.get('/api/server/drivers')
def get_server_drivers():
    """List server drivers"""
    # status = []
    # for driver in indi_server.get_running_drivers():
    #     status.append({'driver': driver})
    # return json.dumps(status)
    # labels = []
    # for label in sorted(indi_server.get_running_drivers().keys()):
    #     labels.append({'driver': label})
    # return json.dumps(labels)
    drivers = []
    if indi_server.is_running() is True:
        for driver in indi_server.get_running_drivers().values():
            drivers.append(driver.__dict__)
    return json.dumps(drivers)


@app.post('/api/server/start/<profile>')
def start_server(profile):
    """Start INDI server for a specific profile"""
    global saved_profile
    saved_profile = profile
    global active_profile
    active_profile = profile
    response.set_cookie("indiserver_profile", profile,
                        None, max_age=3600000, path='/')
    start_profile(profile)


@app.post('/api/server/stop')
def stop_server():
    """Stop INDI Server"""
    indihub_agent.stop()
    indi_server.stop()

    global active_profile
    active_profile = ""

    # If there is saved_profile already let's try to reset it
    global saved_profile
    if saved_profile:
        saved_profile = request.get_cookie("indiserver_profile") or "Simulators"


###############################################################################
# Info endpoints
###############################################################################

@app.get('/api/info/version')
def get_version():    
    return {"version": version("indiweb")}


# Get StellarMate Architecture
@app.get('/api/info/arch')
def get_arch():
    arch = platform.machine()
    if arch == "aarch64":
        arch = "arm64"
    elif arch == "armv7l":
        arch = "armhf"
    return arch

# Get Hostname
@app.get('/api/info/hostname')
def get_hostname():
    return {"hostname": socket.gethostname()}
    
###############################################################################
# Driver endpoints
###############################################################################

@app.get('/api/drivers/groups')
def get_json_groups():
    """Get all driver families (JSON)"""
    response.content_type = 'application/json'
    families = collection.get_families()
    return json.dumps(sorted(families.keys()))


@app.get('/api/drivers')
def get_json_drivers():
    """Get all drivers (JSON)"""
    response.content_type = 'application/json'
    return json.dumps([ob.__dict__ for ob in collection.drivers])


@app.post('/api/drivers/start/<label>')
def start_driver(label):
    """Start INDI driver"""
    driver = collection.by_label(label)
    indi_server.start_driver(driver)
    logging.info('Driver "%s" started.' % label)

@app.post('/api/drivers/start_remote/<label>')
def start_remote_driver(label):
    """Start INDI driver"""
    driver = DeviceDriver(label, label, "1.0", label, "Remote")
    indi_server.start_driver(driver)
    logging.info('Driver "%s" started.' % label)

@app.post('/api/drivers/stop/<label>')
def stop_driver(label):
    """Stop INDI driver"""
    driver = collection.by_label(label)
    indi_server.stop_driver(driver)
    logging.info('Driver "%s" stopped.' % label)

@app.post('/api/drivers/stop_remote/<label>')
def stop_remote_driver(label):
    """Stop INDI driver"""
    driver = DeviceDriver(label, label, "1.0", label, "Remote")
    indi_server.stop_driver(driver)
    logging.info('Driver "%s" stopped.' % label)


@app.post('/api/drivers/restart/<label>')
def restart_driver(label):
    """Restart INDI driver"""
    driver = collection.by_label(label)
    indi_server.stop_driver(driver)
    indi_server.start_driver(driver)
    logging.info('Driver "%s" restarted.' % label)

###############################################################################
# Device endpoints
###############################################################################


@app.get('/api/devices')
def get_devices():
    return json.dumps(indi_device.get_devices())

###############################################################################
# System control endpoints
###############################################################################


@app.post('/api/system/reboot')
def system_reboot():
    """reboot the system running indi-web"""
    logging.info('System reboot, stopping server...')
    stop_server()
    logging.info('rebooting...')
    subprocess.run(["sudo", "reboot"] if args.sudo else "reboot")


@app.post('/api/system/poweroff')
def system_poweroff():
    """poweroff the system"""
    logging.info('System poweroff, stopping server...')
    stop_server()
    logging.info('poweroff...')
    subprocess.run(["sudo", "poweroff"] if args.sudo else "poweroff")

###############################################################################
# INDIHUB Agent control endpoints
###############################################################################


@app.get('/api/indihub/status')
def get_indihub_status():
    """INDIHUB Agent status"""
    mode = indihub_agent.get_mode()
    is_running = indihub_agent.is_running()
    response.content_type = 'application/json'
    status = [{'status': str(is_running), 'mode': mode, 'active_profile': active_profile}]
    return json.dumps(status)


@app.post('/api/indihub/mode/<mode>')
def change_indihub_agent_mode(mode):
    """Change INDIHUB Agent mode with a current INDI-profile"""

    if active_profile == "" or not indi_server.is_running():
        response.content_type = 'application/json'
        response.status = 500
        return json.dumps({'message': 'INDI-server is not running. You need to run INDI-server first.'})

    if indihub_agent.is_running():
        indihub_agent.stop()

    if mode == 'off':
        return

    indihub_agent.start(active_profile, mode)


###############################################################################
# Startup standalone server
###############################################################################


def main():
    """Start autostart profile if any"""
    global active_profile

    for profile in db.get_profiles():
        if profile['autostart']:
            start_profile(profile['name'])
            active_profile = profile['name']
            break

    run(app, host=args.host, port=args.port, quiet=args.verbose)
    logging.info("Exiting")


# JM 2018-12-24: Added __main__ so I can debug this as a module in PyCharm
# Otherwise, I couldn't get it to run main as all
if __name__ == '__init__' or __name__ == '__main__':
    main()
