"""
    Copyright 2016 Inmanta

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Contact: code@inmanta.com
"""

import logging
import os
import re
import urllib

from inmanta import data
from inmanta.agent.handler import provider, ResourceHandler, HandlerContext, CRUDHandler, ResourcePurged
from inmanta.execute.util import Unknown
from inmanta.resources import Resource, PurgeableResource, resource, ResourceNotFoundExcpetion, IgnoreResourceException

import hashlib

def hash_file(content):
    """
        Create a hash from the given content
    """
    sha1sum = hashlib.new("sha1")
    sha1sum.update(content)

    return sha1sum.hexdigest()

LOGGER = logging.getLogger(__name__)


def generate_content(content_list, seperator):
    """
        Generate a sorted list of content to prefix or suffix a file
    """
    sort_list = []
    for content in content_list:
        if content.sorting_key is None:
            sort_list.append((content.value, content.value))
        else:
            sort_list.append((content.sorting_key, content.value))

    sort_list.sort(key=lambda tup: tup[0])
    return seperator.join([x[1] for x in sort_list]) + seperator


def store_file(exporter, obj):
    content = obj.content
    if isinstance(content, Unknown):
        return content

    if "FileMarker" in content.__class__.__name__ :
        with open(content.filename,"rb") as fd:
            content = fd.read()

    if len(obj.prefix_content) > 0:
        content = generate_content(obj.prefix_content, obj.content_seperator) + obj.content_seperator + content
    if len(obj.suffix_content) > 0:
        content += obj.content_seperator + generate_content(obj.suffix_content, obj.content_seperator)

    return exporter.upload_file(content)


@resource("std::Service", agent="host.name", id_attribute="name")
class Service(Resource):
    """
        This class represents a service on a system.
    """
    fields = ("onboot", "state", "name", "reload")


@resource("std::File", agent="host.name", id_attribute="path")
class File(PurgeableResource):
    """
        A file on a filesystem
    """
    fields = ("path", "owner", "hash", "group", "permissions", "reload")
    map = {"hash": store_file, "permissions": lambda y, x: int(x.mode)}


@resource("std::Directory", agent="host.name", id_attribute="path")
class Directory(PurgeableResource):
    """
        A directory on a filesystem
    """
    fields = ("path", "owner", "group", "permissions", "reload")
    map = {"permissions": lambda y, x: int(x.mode)}


@resource("std::Package", agent="host.name", id_attribute="name")
class Package(Resource):
    """
        A software package installed on an operating system.
    """
    fields = ("name", "state", "reload")


@resource("std::Symlink", agent="host.name", id_attribute="target")
class Symlink(PurgeableResource):
    """
        A symbolic link on the filesystem
    """
    fields = ("source", "target", "reload")


@resource("std::AgentConfig", agent="agent", id_attribute="agentname")
class AgentConfig(PurgeableResource):
    """
        A resource that can modify the agentmap for autostarted agents
    """
    fields = ("agentname", "uri", "autostart")

    @staticmethod
    def get_autostart(exp, obj):
        try:
            if not obj.autostart:
                raise IgnoreResourceException()
        except Exception as e:
            # When this attribute is not set, also ignore it
            raise IgnoreResourceException()
        return obj.autostart


@provider("std::File", name="posix_file")
class PosixFileProvider(CRUDHandler):
    """
        This handler can deploy files on a unix system
    """
    def read_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        if not self._io.file_exists(resource.path):
            raise ResourcePurged()

        resource.hash = self._io.hash_file(resource.path)

        # upload the previous version for back up and for generating a diff!
        content = self._io.read_binary(resource.path)

        if not self.stat_file(resource.hash):
            self.upload_file(resource.hash, content)

        for key, value in self._io.file_stat(resource.path).items():
            setattr(resource, key, value)

    def create_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        if self._io.file_exists(resource.path):
            raise Exception(f"Cannot create file {resource.path}, because it already exists.")

        data = self.get_file(resource.hash)
        if hash_file(data) != resource.hash:
            raise Exception("File hash was %s expected %s" % (resource.hash, hash_file(data)))

        self._io.put(resource.path, data)
        self._io.chmod(resource.path, str(resource.permissions))
        self._io.chown(resource.path, resource.owner, resource.group)
        ctx.set_created()

    def delete_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        if self._io.file_exists(resource.path):
            self._io.remove(resource.path)
            ctx.set_purged()

    def update_resource(self, ctx: HandlerContext, changes: dict, resource: PurgeableResource) -> None:
        if not self._io.file_exists(resource.path):
            raise Exception(f"Cannot update file {resource.path} because it doesn't exist")

        if "hash" in changes:
            data = self.get_file(resource.hash)
            if hash_file(data) != resource.hash:
                raise Exception("File hash was %s expected %s" % (resource.hash, hash_file(data)))
            self._io.put(resource.path, data)

        if "permissions" in changes:
            self._io.chmod(resource.path, str(resource.permissions))

        if "owner" in changes or "group" in changes:
            self._io.chown(resource.path, resource.owner, resource.group)
        ctx.set_updated()


@provider("std::Service", name="systemd")
class SystemdService(ResourceHandler):
    """
        A handler for services on systems that use systemd
    """
    def __init__(self, agent, io=None):
        super().__init__(agent, io)

        self._systemd_path = None

    def available(self, resource):
        if self._io.file_exists("/usr/bin/systemctl"):
            self._systemd_path = "/usr/bin/systemctl"

        elif self._io.file_exists("/bin/systemctl"):
            self._systemd_path = "/bin/systemctl"

        return self._systemd_path is not None

    def check_resource(self, ctx, resource):
        current = resource.clone()

        exists = self._io.run(self._systemd_path, ["status", "%s.service" % resource.name])[0]

        if re.search('Loaded: error', exists):
            raise ResourceNotFoundExcpetion("The %s service does not exist" % resource.name)

        running = self._io.run(self._systemd_path, ["is-active", "%s.service" % resource.name])[2] == 0
        enabled = self._io.run(self._systemd_path, ["is-enabled", "%s.service" % resource.name])[2] == 0

        if running:
            current.state = "running"
        else:
            current.state = "stopped"

        current.onboot = enabled
        return current

    def can_reload(self):
        """
            Can this handler reload?
        """
        return True

    def do_reload(self, ctx, resource):
        """
            Reload this resource
        """
        ctx.info("Reloading service with reload-or-restart")
        self._io.run(self._systemd_path, ["reload-or-restart", "%s.service" % resource.name])

    def do_changes(self, ctx, resource, changes):
        updated = False
        if "state" in changes:
            action = "start"
            if changes["state"]["desired"] == "stopped":
                action = "stop"

            # start or stop the service
            result = self._io.run(self._systemd_path, [action, "%s.service" % resource.name])

            if re.search("^Failed", result[1]):
                raise Exception("Unable to %s %s: %s" % (action, resource.name, result[1]))

            updated = True

        if "onboot" in changes:
            action = "enable"

            if not changes["onboot"]["desired"]:
                action = "disable"

            result = self._io.run(self._systemd_path, [action, "%s.service" % resource.name])
            updated = True

            if re.search("^Failed", result[1]):
                raise Exception("Unable to %s %s: %s" % (action, resource.name, result[1]))

        if updated:
            ctx.set_updated()


@provider("std::Service", name="redhat_service")
class ServiceService(ResourceHandler):
    """
        A handler for services on systems that use service
    """
    def available(self, resource):
        return (self._io.file_exists("/sbin/chkconfig") and self._io.file_exists("/sbin/service") and
                not self._io.file_exists("/usr/bin/systemctl"))

    def check_resource(self, ctx, resource):
        current = resource.clone()
        exists = self._io.run("/sbin/chkconfig", ["--list", resource.name])[0]

        if re.search('error reading information on service', exists):
            raise ResourceNotFoundExcpetion("The %s service does not exist" % resource.name)


        raw_enabled = self._io.run("/sbin/chkconfig", ["--list", resource.name])[0]
        enabled = ":on" in raw_enabled
        running = self._io.run("/sbin/service", [resource.name, "status"])[2] == 0

        current.onboot = enabled
        if running:
            current.state = "running"
        else:
            current.state = "stopped"

        return current

    def can_reload(self):
        """
            Can this handler reload?
        """
        return True

    def do_reload(self, ctx, resource):
        """
            Reload this resource
        """
        (o, e, ret) = self._io.run("/sbin/service", [resource.name, "reload"])
        if ret != 0:
            ctx.info("could not reload! %(ret)s %(out)s %(err)s", ret=ret, out=o, err=e)

    def do_changes(self, ctx, resource, changes):
        updated = False
        if "state" in changes:
            action = "start"
            if changes["state"]["desired"] == "stopped":
                action = "stop"

            # start or stop the service
            result = self._io.run("/sbin/service", [resource.name, action])

            if re.search("^Failed", result[1]):
                raise Exception("Unable to %s %s: %s" % (action, resource.name, result[1]))

            updated = True

        if "onboot" in changes:
            action = "on"

            if not changes["onboot"]["desired"]:
                action = "off"

            ctx.debug("Performing /sbin/chkconfig %(args)s", args=[resource.name, action])

            result = self._io.run("/sbin/chkconfig", [resource.name, action])
            updated = True

            if re.search("^Failed", result[1]):
                raise Exception("Unable to %s %s: %s" % (action, resource.name, result[1]))

        if updated:
            ctx.set_updated()


@provider("std::Package", name="yum")
class YumPackage(ResourceHandler):
    """
        A Package handler that uses yum
    """
    def available(self, resource):
        return (self._io.file_exists("/usr/bin/rpm") or self._io.file_exists("/bin/rpm")) \
            and (self._io.file_exists("/usr/bin/yum") or self._io.file_exists("/usr/bin/dnf"))

    def _parse_fields(self, lines):
        props = {}
        key = ""
        old_key = None
        for line in lines:
            if line.strip() == "":
                continue

            if line.strip() == "Available Packages":
                break

            result = re.search(r"""^(.+) :\s+(.+)""", line)
            if result is None:
                continue

            key, value = result.groups()
            key = key.strip()

            if key == "":
                props[old_key] += " " + value
            else:
                props[key] = value
                old_key = key

        return props

    def _run_yum(self, args):
        # todo: cache value
        if self._io.file_exists("/usr/bin/dnf"):
            return self._io.run("/usr/bin/dnf", ["-d", "0", "-e", "0", "-y"] + args)
        else:
            return self._io.run("/usr/bin/yum", ["-d", "0", "-e", "0", "-y"] + args)

    def check_resource(self, ctx, resource):
        yum_output = self._run_yum(["info", resource.name])
        lines = yum_output[0].split("\n")

        output = self._parse_fields(lines[1:])

        if "Repo" not in output:
            return {"state": "removed"}

        state = "removed"

        if output["Repo"] == "installed" or output["Repo"] == "@System":
            state = "installed"

        # check if there is an update
        yum_output = self._run_yum(["check-update", resource.name])
        lines = yum_output[0].split("\n")

        data = {"state": state, "version": output["Version"],
                "release": output["Release"], "update": None}

        if len(lines) > 0:
            parts = re.search(r"""([^\s]+)\s+([^\s]+)\s+([^\s]+)""", lines[0])
            if parts is not None and not lines[0].startswith("Security:"):
                version_str = parts.groups()[1]
                version, release = version_str.split("-")

                data["update"] = (version, release)

        return data

    def list_changes(self, ctx, resource):
        state = self.check_resource(ctx, resource)

        changes = {}
        if resource.state == "removed":
            if state["state"] != "removed":
                changes["state"] = (state["state"], resource.state)

        elif resource.state == "installed" or resource.state == "latest":
            if state["state"] != "installed":
                changes["state"] = (state["state"], "installed")

        if "update" in state and state["update"] is not None and resource.state == "latest":
            changes["version"] = ((state["version"], state["release"]), state["update"])

        return changes

    def _result(self, output):
        stdout = output[0].strip()
        error_msg = output[1].strip()
        if output[2] != 0:
            raise Exception("Yum failed: stdout:" + stdout + " errout: " + error_msg)

    def do_changes(self, ctx, resource, changes):
        if "state" in changes:
            if changes["state"][1] == "removed":
                self._result(self._run_yum(["remove", resource.name]))
                ctx.set_purged()

            elif changes["state"][1] == "installed":
                self._result(self._run_yum(["install", resource.name]))
                self._result(self._run_yum(["update", resource.name]))
                ctx.set_created()

        elif "version" in changes:
            self._result(self._run_yum(["update", resource.name]))
            ctx.set_updated()


@provider("std::Directory", name="posix_directory")
class DirectoryHandler(CRUDHandler):
    """
        A handler for creating directories

        TODO: add recursive operations
    """
    def read_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        if not self._io.file_exists(resource.path):
            raise ResourcePurged()
        else:
            for key, value in self._io.file_stat(resource.path).items():
                setattr(resource, key, value)

    def create_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        self._io.mkdir(resource.path)
        mode = str(resource.permissions)
        self._io.chmod(resource.path, mode)
        self._io.chown(resource.path, resource.owner, resource.group)
        ctx.set_created()

    def delete_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        self._io.rmdir(resource.path)
        ctx.set_purged()

    def update_resource(self, ctx: HandlerContext, changes: dict, resource: PurgeableResource) -> None:
        if "permissions" in changes:
            mode = str(resource.permissions)
            self._io.chmod(resource.path, mode)
        if "owner" in changes or "group" in changes:
            self._io.chown(resource.path, resource.owner, resource.group)
        ctx.set_updated()


@provider("std::Symlink", name="posix_symlink")
class SymlinkProvider(CRUDHandler):
    """
        This handler can deploy symlinks on unix systems
    """
    def available(self, resource):
        return self._io.file_exists("/usr/bin/ln") or self._io.file_exists("/bin/ln")

    def read_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        if not self._io.file_exists(resource.target):
            raise ResourcePurged()
        elif not self._io.is_symlink(resource.target):
            raise Exception("The target of resource %s already exists but is not a symlink." % resource)
        else:
            resource.source = self._io.readlink(resource.target)

    def create_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        self._io.symlink(resource.source, resource.target)
        ctx.set_created()

    def delete_resource(self, ctx: HandlerContext, resource: PurgeableResource) -> None:
        self._io.remove(resource.target)
        ctx.set_purged()

    def update_resource(self, ctx: HandlerContext, changes: dict, resource: PurgeableResource) -> None:
        self._io.remove(resource.target)
        self._io.symlink(resource.source, resource.target)
        ctx.set_updated()


@provider("std::AgentConfig", name="agentrest")
class AgentConfigHandler(CRUDHandler):
    def _get_map(self) -> dict:
        def call():
            return self.get_client().get_setting(tid=self._agent.environment, id=data.AUTOSTART_AGENT_MAP)

        value = self.run_sync(call)
        return value.result["value"]

    def _set_map(self, agent_config: dict) -> None:
        def call():
            return self.get_client().set_setting(tid=self._agent.environment, id=data.AUTOSTART_AGENT_MAP, value=agent_config)

        return self.run_sync(call)

    def read_resource(self, ctx: HandlerContext, resource: AgentConfig) -> None:
        agent_config = self._get_map()
        ctx.set("map", agent_config)

        if resource.agentname not in agent_config:
            raise ResourcePurged()

        resource.uri = agent_config[resource.agentname]

    def create_resource(self, ctx: HandlerContext, resource: AgentConfig) -> None:
        agent_config = ctx.get("map")
        agent_config[resource.agentname] = resource.uri
        self._set_map(agent_config)

    def delete_resource(self, ctx: HandlerContext, resource: AgentConfig) -> None:
        agent_config = ctx.get("map")
        del agent_config[resource.agentname]
        self._set_map(agent_config)

    def update_resource(self, ctx: HandlerContext, changes: dict, resource: AgentConfig) -> None:
        agent_config = ctx.get("map")
        agent_config[resource.agentname] = resource.uri
        self._set_map(agent_config)
