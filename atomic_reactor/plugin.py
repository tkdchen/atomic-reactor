"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


definition of plugin system

plugins are supposed to be run when image is built and we need to extract some information
"""
from __future__ import absolute_import

import copy
import logging
import os
import sys
import traceback
import imp
import datetime
import inspect
import time
from six import PY2
from collections import namedtuple

from atomic_reactor.build import BuildResult
from atomic_reactor.util import process_substitutions, exception_message
from dockerfile_parse import DockerfileParser

MODULE_EXTENSIONS = ('.py', '.pyc', '.pyo')
logger = logging.getLogger(__name__)


class AutoRebuildCanceledException(Exception):
    """Raised if a plugin cancels autorebuild"""
    def __init__(self, plugin_key, msg):
        self.plugin_key = plugin_key
        self.msg = msg

    def __str__(self):
        return 'plugin %s canceled autorebuild: %s' % (self.plugin_key, self.msg)


class PluginFailedException(Exception):
    """ There was an error during plugin execution """


class BuildCanceledException(Exception):
    """Build was canceled"""


class InappropriateBuildStepError(Exception):
    """Requested build step is not appropriate"""


class Plugin(object):
    """ abstract plugin class """

    # unique plugin identification
    # output of this plugin can be found in results specified with this key,
    # same thing goes for input: use this key for providing input for this plugin
    key = None
    # by default, if plugin fails (raises exc), execution continues
    is_allowed_to_fail = True

    def __init__(self, *args, **kwargs):
        """
        constructor
        """
        self.log = logging.getLogger("atomic_reactor.plugins." + self.key)
        self.args = args
        self.kwargs = kwargs

    def __str__(self):
        return "%s" % self.key

    def __repr__(self):
        return "Plugin(key='%s')" % self.key

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from a build plugin is kept and used in json result response like this:

          results[plugin.key] = plugin.run()

        input plugins should emit build json with this method
        """
        raise NotImplementedError()


class BuildPlugin(Plugin):
    """
    abstract plugin class: base for build plugins, it is
    flavored with ContainerTasker and BuildWorkflow instances
    """

    def __init__(self, tasker, workflow, *args, **kwargs):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param args: arguments from user input
        :param kwargs: keyword arguments from user input
        """
        self.tasker = tasker
        self.workflow = workflow
        super(BuildPlugin, self).__init__(*args, **kwargs)

    def is_in_orchestrator(self):
        """
        Check if the configuration this plugin is part of is for
        an orchestrator build or a worker build.

        :return: True if orchestrator build, False if worker build
        """
        return self.workflow.is_orchestrator_build()


class PluginsRunner(object):

    def __init__(self, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: list of dicts, configuration for plugins
        """
        self.plugins_results = getattr(self, "plugins_results", {})
        self.plugins_conf = plugins_conf or []
        self.plugin_files = kwargs.get("plugin_files", [])
        self.plugin_classes = self.load_plugins(plugin_class_name)
        self.available_plugins = self.get_available_plugins()

    def load_plugins(self, plugin_class_name):
        """
        load all available plugins

        :param plugin_class_name: str, name of plugin class (e.g. 'PreBuildPlugin')
        :return: dict, bindings for plugins of the plugin_class_name class
        """
        # imp.findmodule('atomic_reactor') doesn't work
        plugins_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        logger.debug("loading plugins from dir '%s'", plugins_dir)
        files = [os.path.join(plugins_dir, f)
                 for f in os.listdir(plugins_dir)
                 if f.endswith(".py")]
        if self.plugin_files:
            logger.debug("loading additional plugins from files '%s'", self.plugin_files)
            files += self.plugin_files
        plugin_class = globals()[plugin_class_name]
        plugin_classes = {}
        for f in files:
            module_name = os.path.basename(f).rsplit('.', 1)[0]
            # Do not reload plugins
            if module_name in sys.modules:
                f_module = sys.modules[module_name]
            else:
                try:
                    logger.debug("load file '%s'", f)
                    f_module = imp.load_source(module_name, f)
                except (IOError, OSError, ImportError, SyntaxError) as ex:
                    logger.warning("can't load module '%s': %s", f, ex)
                    continue
            for name in dir(f_module):
                binding = getattr(f_module, name, None)
                try:
                    # if you try to compare binding and PostBuildPlugin, python won't match them
                    # if you call this script directly b/c:
                    # ! <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class
                    # '__main__.PostBuildPlugin'>
                    # but
                    # <class 'plugins.plugin_rpmqa.PostBuildRPMqaPlugin'> <= <class
                    # 'atomic_reactor.plugin.PostBuildPlugin'>
                    is_sub = issubclass(binding, plugin_class)
                except TypeError:
                    is_sub = False
                if binding and is_sub and plugin_class.__name__ != binding.__name__:
                    plugin_classes[binding.key] = binding
        return plugin_classes

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        """
        create instance from plugin using the plugin class and configuration passed to for it

        :param plugin_class: plugin class
        :param plugin_conf: dict, configuration for plugin
        :return:
        """
        plugin_instance = plugin_class(**plugin_conf)
        return plugin_instance

    def on_plugin_failed(self, plugin=None, exception=None):
        pass

    def save_plugin_timestamp(self, plugin, timestamp):
        pass

    def save_plugin_duration(self, plugin, duration):
        pass

    def get_available_plugins(self):
        """
        check requested plugins availability
        and handle missing plugins

        :return: list of namedtuples, runnable plugins data
        """
        available_plugins = []
        PluginData = namedtuple('PluginData', 'name, plugin_class, conf, is_allowed_to_fail')
        for plugin_request in self.plugins_conf:
            plugin_name = plugin_request['name']
            try:
                plugin_class = self.plugin_classes[plugin_name]
            except KeyError:
                if plugin_request.get('required', True):
                    msg = ("no such plugin: '%s', did you set "
                           "the correct plugin type?") % plugin_name
                    exc = PluginFailedException(msg)
                    self.on_plugin_failed(plugin_name, exc)
                    logger.error(msg)
                    raise exc
                else:
                    # This plugin is marked as not being required
                    logger.warning("plugin '%s' requested but not available",
                                   plugin_name)
                    continue
            plugin_is_allowed_to_fail = plugin_request.get('is_allowed_to_fail',
                                                           getattr(plugin_class,
                                                                   "is_allowed_to_fail", True))
            plugin_conf = plugin_request.get("args", {})
            plugin = PluginData(plugin_name,
                                plugin_class,
                                plugin_conf,
                                plugin_is_allowed_to_fail)
            available_plugins.append(plugin)
        return available_plugins

    def run(self, keep_going=False, buildstep_phase=False):
        """
        run all requested plugins

        :param keep_going: bool, whether to keep going after unexpected
                                 failure (only used for exit plugins)
        :param buildstep_phase: bool, when True remaining plugins will
                                not be executed after a plugin completes
                                (only used for build-step plugins)
        """
        failed_msgs = []
        plugin_successful = False
        plugin_response = None
        available_plugins = self.available_plugins
        for plugin in available_plugins:
            plugin_successful = False

            logger.debug("running plugin '%s'", plugin.name)
            start_time = datetime.datetime.now()

            plugin_response = None
            skip_response = False
            try:
                plugin_instance = self.create_instance_from_plugin(plugin.plugin_class,
                                                                   plugin.conf)
                self.save_plugin_timestamp(plugin.plugin_class.key, start_time)
                plugin_response = plugin_instance.run()
                plugin_successful = True
                if buildstep_phase:
                    assert isinstance(plugin_response, BuildResult)
                    if plugin_response.is_failed():
                        logger.error("Build step plugin %s failed: %s",
                                     plugin.plugin_class.key,
                                     plugin_response.fail_reason)
                        self.on_plugin_failed(plugin.plugin_class.key,
                                              plugin_response.fail_reason)
                        plugin_successful = False
                        self.plugins_results[plugin.plugin_class.key] = plugin_response
                        break

            except AutoRebuildCanceledException as ex:
                # if auto rebuild is canceled, then just reraise
                # NOTE: We need to catch and reraise explicitly, so that the below except clause
                #   doesn't catch this and make PluginFailedException out of it in the end
                #   (calling methods would then need to parse exception message to see if
                #   AutoRebuildCanceledException was raised here)
                raise
            except InappropriateBuildStepError:
                logger.debug('Build step %s is not appropriate', plugin.plugin_class.key)
                # don't put None, in results for InappropriateBuildStepError
                skip_response = True
                if not buildstep_phase:
                    raise
            except Exception as ex:
                msg = "plugin '%s' raised an exception: %s" % (plugin.plugin_class.key,
                                                               exception_message(ex))
                logger.debug(traceback.format_exc())
                if not plugin.is_allowed_to_fail:
                    self.on_plugin_failed(plugin.plugin_class.key, ex)

                if plugin.is_allowed_to_fail or keep_going:
                    logger.warning(msg)
                    logger.info("error is not fatal, continuing...")
                    if not plugin.is_allowed_to_fail:
                        failed_msgs.append(msg)
                else:
                    logger.error(msg)
                    raise PluginFailedException(msg)

                plugin_response = ex

            try:
                if start_time:
                    finish_time = datetime.datetime.now()
                    duration = finish_time - start_time
                    seconds = duration.total_seconds()
                    logger.debug("plugin '%s' finished in %ds", plugin.name, seconds)
                    self.save_plugin_duration(plugin.plugin_class.key, seconds)
            except Exception:
                logger.exception("failed to save plugin duration")

            if not skip_response:
                self.plugins_results[plugin.plugin_class.key] = plugin_response

            if plugin_successful and buildstep_phase:
                logger.debug('stopping further execution of plugins '
                             'after first successful plugin')
                break

        if len(failed_msgs) == 1:
            logger.exception('something wrong in plugin, re-raise it.')
            raise PluginFailedException(failed_msgs[0])
        elif len(failed_msgs) > 1:
            logger.exception('something wrong in plugin, re-raise it.')
            raise PluginFailedException("Multiple plugins raised an exception: " +
                                        str(failed_msgs))

        if not plugin_successful and buildstep_phase and not plugin_response:
            self.on_plugin_failed("BuildStepPlugin", "No appropriate build step")
            raise PluginFailedException("No appropriate build step")

        return self.plugins_results


class BuildPluginsRunner(PluginsRunner):
    def __init__(self, dt, workflow, plugin_class_name, plugins_conf, *args, **kwargs):
        """
        constructor

        :param dt: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param plugin_class_name: str, name of plugin class to filter (e.g. 'PreBuildPlugin')
        :param plugins_conf: list of dicts, configuration for plugins
        """
        self.dt = dt
        self.workflow = workflow
        super(BuildPluginsRunner, self).__init__(plugin_class_name, plugins_conf, *args, **kwargs)

    def on_plugin_failed(self, plugin=None, exception=None):
        self.workflow.plugin_failed = True
        if plugin and exception:
            self.workflow.plugins_errors[plugin] = str(exception)

    def save_plugin_timestamp(self, plugin, timestamp):
        self.workflow.plugins_timestamps[plugin] = timestamp.isoformat()

    def save_plugin_duration(self, plugin, duration):
        self.workflow.plugins_durations[plugin] = duration

    def _translate_special_values(self, obj_to_translate):
        """
        you may want to write plugins for values which are not known before build:
        e.g. id of built image, base image name,... this method will therefore
        translate some reserved values to the runtime values
        """
        translation_dict = {
            'BUILT_IMAGE_ID': self.workflow.builder.image_id,
            'BUILD_DOCKERFILE_PATH': self.workflow.builder.source.dockerfile_path,
            'BUILD_SOURCE_PATH':  self.workflow.builder.source.path,
        }

        if self.workflow.builder.base_image:
            translation_dict['BASE_IMAGE'] = self.workflow.builder.base_image.to_str()

        if isinstance(obj_to_translate, dict):
            # Recurse into dicts
            translated_dict = copy.deepcopy(obj_to_translate)
            for key, value in obj_to_translate.items():
                translated_dict[key] = self._translate_special_values(value)

            return translated_dict
        elif isinstance(obj_to_translate, list):
            # Iterate over lists
            return [self._translate_special_values(elem)
                    for elem in obj_to_translate]
        else:
            return translation_dict.get(obj_to_translate, obj_to_translate)

    def _remove_unknown_args(self, plugin_class, plugin_conf):
        if PY2:
            sig = inspect.getargspec(plugin_class.__init__)  # pylint: disable=deprecated-method
            kwargs = sig.keywords
        else:
            sig = inspect.getfullargspec(plugin_class.__init__)  # pylint: disable=no-member
            kwargs = sig.varkw

        # Constructor defines **kwargs, it'll take any parameter
        if kwargs:
            return plugin_conf

        args = set(sig.args)
        known_plugin_conf = {}
        for key, value in plugin_conf.items():
            if key not in args:
                logger.warning(
                    '%s constructor does not take %s=%s parameter, ignoring it',
                    plugin_class.__name__, key, value)
                continue
            known_plugin_conf[key] = value

        return known_plugin_conf

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        plugin_conf = self._translate_special_values(plugin_conf)
        plugin_conf = self._remove_unknown_args(plugin_class, plugin_conf)
        logger.info("running plugin instance with args: '%s'", plugin_conf)
        plugin_instance = plugin_class(self.dt, self.workflow, **plugin_conf)
        return plugin_instance


class PreBuildPlugin(BuildPlugin):
    pass


class PreBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-build plugins")
        self.plugins_results = workflow.prebuild_results
        super(PreBuildPluginsRunner, self).__init__(dt, workflow, 'PreBuildPlugin', plugins_conf,
                                                    *args, **kwargs)


class BuildStepPlugin(BuildPlugin):
    pass


class BuildStepPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugin_conf, *args, **kwargs):
        logger.info("initializing runner of build-step plugin")
        self.plugins_results = workflow.buildstep_result

        if plugin_conf:
            # any non existing buildstep plugin must be skipped without error
            for plugin in plugin_conf:
                plugin['required'] = False
                plugin['is_allowed_to_fail'] = False
        else:
            # if no buildstep_plugins configured, which is typical for worker builds,
            # use what the source says or the system default.
            source_method = workflow.builder.source.config.image_build_method
            system_method = workflow.default_image_build_method
            plugin_conf = [{'name': source_method or system_method, 'is_allowed_to_fail': False}]

        super(BuildStepPluginsRunner, self).__init__(
            dt, workflow, 'BuildStepPlugin', plugin_conf, *args, **kwargs)

    def run(self, *args, **kwargs):
        builder = self.workflow.builder

        logger.info('building image %r inside current environment',
                    builder.image)
        builder.ensure_not_built()
        if builder.df_path:
            logger.debug('using dockerfile:\n%s',
                         DockerfileParser(builder.df_path).content)
        else:
            logger.debug("No Dockerfile path has been specified")

        kwargs['buildstep_phase'] = True

        plugins_results = super(BuildStepPluginsRunner, self).run(*args, **kwargs)
        return list(plugins_results.values())[0]


class PrePublishPlugin(BuildPlugin):
    pass


class PrePublishPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of pre-publish plugins")
        self.plugins_results = workflow.prepub_results
        super(PrePublishPluginsRunner, self).__init__(dt, workflow, 'PrePublishPlugin',
                                                      plugins_conf, *args, **kwargs)


class PostBuildPlugin(BuildPlugin):
    pass


class PostBuildPluginsRunner(BuildPluginsRunner):

    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of post-build plugins")
        self.plugins_results = workflow.postbuild_results
        super(PostBuildPluginsRunner, self).__init__(dt, workflow, 'PostBuildPlugin',
                                                     plugins_conf, *args, **kwargs)

    def create_instance_from_plugin(self, plugin_class, plugin_conf):
        instance = super(PostBuildPluginsRunner, self).create_instance_from_plugin(plugin_class,
                                                                                   plugin_conf)

        return instance


class ExitPlugin(PostBuildPlugin):
    """
    Plugin base class for plugins which should be run just before
    exit. It is flavored with ContainerTasker and DockerBuildWorkflow instances.
    """


class ExitPluginsRunner(BuildPluginsRunner):
    def __init__(self, dt, workflow, plugins_conf, *args, **kwargs):
        logger.info("initializing runner of exit plugins")
        self.plugins_results = workflow.exit_results
        super(ExitPluginsRunner, self).__init__(dt, workflow, 'ExitPlugin',
                                                plugins_conf, *args, **kwargs)


class InputPlugin(Plugin):

    def __init__(self, substitutions=None, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(InputPlugin, self).__init__(**kwargs)
        self.substitutions = substitutions

    def substitute_configuration(self, build_json):
        """
        replace values of provided build json according to self.substitutions

        path to values can be specified in two ways:

         * single key value for root arguments, e.g. 'image'
         * plugin configuration: you following convention:

             plugin_type.plugin_name.argument_name

           hence

             prebuild_plugins.koji.target

        :param build_json: dict, build json
        :return: dict, substituted build json
        """
        process_substitutions(build_json, self.substitutions)
        return build_json

    @classmethod
    def is_autousable(cls):
        """
        Determine if this plugin can run without providing any further user input,
        e.g. if expected default environment variables are defined, if expected default
        files exist etc

        :return: True if this plugin is autousable, False otherwise
        """
        raise NotImplementedError('is_autousable not implemented in {0}'.format(cls))


class InputPluginsRunner(PluginsRunner):
    def __init__(self, plugins_conf, *args, **kwargs):
        """Wrap `PluginsRunner.__init__()` while implementing the `auto` input behaviour.

        If input plugin name is `auto`, then call `is_autousable` on all input plugins.
        Assuming exactly one of these returns `True`, then use that as input plugin, else raise.
        """
        plugin_class_name = 'InputPlugin'
        self.autoinput = plugins_conf[0]['name'] == 'auto'
        # implement the "auto" input behavior
        if self.autoinput:
            logger.debug('"auto" input used, determining what input plugin to use.')
            autousable = None
            self.plugin_files = kwargs.get("plugin_files", [])
            plugin_classes = self.load_plugins(plugin_class_name)
            for clsname, clsobj in plugin_classes.items():
                logger.debug('checking if "%s" plugin is autousable ...', clsname)
                if clsobj.is_autousable():
                    if autousable:
                        raise PluginFailedException('More than one usable plugin with "auto" '
                                                    'input: {0}, {1}. Please specify --input '
                                                    'explicitly.'.format(autousable, clsname))
                    else:
                        autousable = clsname
            if not autousable:
                raise PluginFailedException('No autousable input plugin. '
                                            'Please specify --input explicitly')
            logger.debug('using "%s" for input', autousable)
            plugins_conf[0]['name'] = autousable

        super(InputPluginsRunner, self).__init__(plugin_class_name, plugins_conf, *args, **kwargs)
        self.plugins_results = {}

    def run(self, *args, **kwargs):
        result = super(InputPluginsRunner, self).run(*args, **kwargs)

        if self.autoinput:
            autousable = self.plugins_conf[0]['name']
            result['auto'] = result.pop(autousable)
        return result


# Built-in plugins
class PreBuildSleepPlugin(PreBuildPlugin):
    """
    Sleep for a specified number of seconds.

    This plugin is only intended to be used for debugging.
    """

    key = 'pre_sleep'

    def __init__(self, tasker, workflow, seconds=60):
        self.seconds = seconds

    def run(self):
        time.sleep(self.seconds)
