import gzip
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List

from briefcase.commands import (
    BuildCommand,
    CreateCommand,
    PackageCommand,
    PublishCommand,
    RunCommand,
    UpdateCommand,
)
from briefcase.config import AppConfig
from briefcase.exceptions import BriefcaseCommandError, UnsupportedHostError
from briefcase.integrations.docker import Docker, DockerAppContext
from briefcase.integrations.subprocess import NativeAppContext
from briefcase.platforms.linux import (
    ARCH,
    DEBIAN,
    RHEL,
    DockerOpenCommand,
    LinuxMixin,
    LocalRequirementsMixin,
    parse_freedesktop_os_release,
)


class LinuxSystemPassiveMixin(LinuxMixin):
    # The Passive mixin honors the Docker options, but doesn't try to verify
    # Docker exists. It is used by commands that are "passive" from the
    # perspective of the build system (e.g., Run).
    output_format = "system"
    supported_host_os = {"Darwin", "Linux"}
    supported_host_os_reason = (
        "Linux system projects can only be built on Linux, or on macOS using Docker."
    )

    @property
    def use_docker(self):
        # The system backend doesn't have a literal "--use-docker" option, but
        # `use_docker` is a useful flag for shared logic purposes, so evaluate
        # what "use docker" means in terms of target_image.
        return bool(self.target_image)

    @property
    def linux_arch(self):
        # Linux uses different architecture identifiers for some platforms
        return {
            "x86_64": "amd64",
        }.get(self.tools.host_arch, self.tools.host_arch)

    def bundle_path(self, app):
        # Override the bundle path to use the app name, rather than formal name
        # This is because Red Hat doesn't like spaces in paths.
        return (
            self.platform_path / app.target_vendor / app.target_codename / app.app_name
        )

    def project_path(self, app):
        return self.bundle_path(app) / f"{app.app_name}-{app.version}"

    def binary_path(self, app):
        return self.project_path(app) / "usr" / "bin" / app.app_name

    def rpm_tag(self, app):
        if app.target_vendor == "fedora":
            return f"fc{app.target_codename}"
        else:
            return f"el{app.target_codename}"

    def distribution_filename(self, app):
        if app.packaging_format == "deb":
            return (
                f"{app.app_name}_{app.version}-{getattr(app, 'revision', 1)}"
                f"~{app.target_vendor}-{app.target_codename}_{self.linux_arch}.deb"
            )
        elif app.packaging_format == "rpm":
            return (
                f"{app.app_name}-{app.version}-{getattr(app, 'revision', 1)}"
                f".{self.rpm_tag(app)}.{self.tools.host_arch}.rpm"
            )
        else:
            raise BriefcaseCommandError(
                "Briefcase doesn't currently know how to build system packages in "
                f"{app.packaging_format.upper()} format."
            )

    def distribution_path(self, app):
        return self.platform_path / self.distribution_filename(app)

    def add_options(self, parser):
        super().add_options(parser)
        parser.add_argument(
            "--target",
            dest="target",
            help="Docker base image tag for the distribution to target for the build (e.g., `ubuntu:jammy`)",
            required=False,
        )

    def parse_options(self, extra):
        """Extract the target_image option."""
        options = super().parse_options(extra)
        self.target_image = options.pop("target")

        return options

    def clone_options(self, command):
        """Clone the target_image option."""
        super().clone_options(command)
        self.target_image = command.target_image

    def target_glibc_version(self, app):
        """Determine the glibc version.

        If running in Docker, this is done by interrogating libc.so.6; outside
        docker, we can use os.confstr().
        """
        if self.use_docker:
            try:
                output = self.tools.docker.check_output(
                    ["ldd", "--version"],
                    image_tag=app.target_image,
                )
                # On Debian/Ubuntu, ldd --version will give you output of the form:
                #
                #     ldd (Ubuntu GLIBC 2.31-0ubuntu9.9) 2.31
                #     Copyright (C) 2020 Free Software Foundation, Inc.
                #     ...
                #
                # Other platforms produce output of the form:
                #
                #     ldd (GNU libc) 2.36
                #     Copyright (C) 2020 Free Software Foundation, Inc.
                #     ...
                #
                # Note that the exact text will vary version to version.
                # Look for the "2.NN" pattern.
                if match := re.search(r"\d\.\d+", output):
                    target_glibc = match.group(0)
                else:
                    raise BriefcaseCommandError(
                        "Unable to parse glibc dependency version from version string."
                    )
            except subprocess.CalledProcessError:
                raise BriefcaseCommandError(
                    "Unable to determine glibc dependency version."
                )

        else:
            target_glibc = self.tools.os.confstr("CS_GNU_LIBC_VERSION").split()[1]

        return target_glibc

    def finalize_app_config(self, app: AppConfig):
        """Finalize app configuration.

        Linux .deb app configurations are deeper than other platforms, because
        they need to include components that are dependent on the target vendor
        and codename. Those properties are extracted from command-line options.

        The final app configuration merges the target-specific configuration
        into the generic "linux.deb" app configuration, as well as setting the
        Python version.

        :param app: The app configuration to finalize.
        """
        self.logger.info("Finalizing application configuration...", prefix=app.app_name)
        if self.use_docker:
            # Preserve the target image on the command line as the app's target
            app.target_image = self.target_image

            # Ensure that the Docker base image is available.
            with self.input.wait_bar(
                f"Checking Docker target image {app.target_image}..."
            ):
                self.tools.docker.prepare(app.target_image)

            output = self.tools.docker.check_output(
                ["cat", "/etc/os-release"],
                image_tag=app.target_image,
            )
            freedesktop_info = parse_freedesktop_os_release(output)
        else:
            if sys.version_info >= (3, 10):
                freedesktop_info = self.tools.platform.freedesktop_os_release()
            else:
                with Path("/etc/os-release").open(encoding="utf-8") as f:
                    freedesktop_info = parse_freedesktop_os_release(f.read())

        # Process the FreeDesktop content to give the vendor, codename and vendor base.
        (
            app.target_vendor,
            app.target_codename,
            app.target_vendor_base,
        ) = self.vendor_details(freedesktop_info)

        self.logger.info(
            f"Targeting {app.target_vendor}:{app.target_codename} (Vendor base {app.target_vendor_base})"
        )

        # Non-docker builds need an app representation of the target image
        # for templating purposes.
        if not self.use_docker:
            app.target_image = f"{app.target_vendor}:{app.target_codename}"

        # Merge target-specific configuration items into the app config This
        # means:
        # * merging app.linux.debian into app, overwriting anything global
        # * merging app.linux.ubuntu into app, overwriting anything vendor-base
        #   specific
        # * merging app.linux.ubuntu.focal into app, overwriting anything vendor
        #   specific
        # The vendor base config (e.g., redhat). The vendor base might not
        # be known, so fall back to an empty vendor config.
        if app.target_vendor_base:
            vendor_base_config = getattr(app, app.target_vendor_base, {})
        else:
            vendor_base_config = {}
        vendor_config = getattr(app, app.target_vendor, {})
        try:
            codename_config = vendor_config[app.target_codename]
        except KeyError:
            codename_config = {}

        # Copy all the specific configurations to the app config
        for config in [
            vendor_base_config,
            vendor_config,
            codename_config,
        ]:
            for key, value in config.items():
                setattr(app, key, value)

        with self.input.wait_bar("Determining glibc version..."):
            app.glibc_version = self.target_glibc_version(app)
        self.logger.info(f"Targeting glibc {app.glibc_version}")

        if self.use_docker:
            # If we're running in Docker, we can't know the Python3 version
            # before rolling out the template; so we fall back to "3"
            app.python_version_tag = "3"
        else:
            # Use the version of Python that run
            app.python_version_tag = self.python_version_tag

        self.logger.info(f"Targeting Python{app.python_version_tag}")


class LinuxSystemMostlyPassiveMixin(LinuxSystemPassiveMixin):
    # The Mostly Passive mixin verifies that Docker exists and can be run, but
    # doesn't require that we're actually in a Linux environment.

    def docker_image_tag(self, app):
        """The Docker image tag for an app."""
        return f"briefcase/{app.bundle}.{app.app_name.lower()}:{app.target_vendor}-{app.target_codename}"

    def verify_tools(self):
        """If we're using Docker, verify that it is available."""
        super().verify_tools()
        if self.use_docker:
            Docker.verify(tools=self.tools)

    def verify_python(self, app):
        """Verify that the version of Python being used to build the app in Docker
        is compatible with the version being used to run Briefcase.

        Will raise an exception if the Python version is fundamentally
        incompatible (i.e., if Briefcase doesn't support it); any other version
        discrepancy will log a warning, but continue.

        Requires that the app tools have been verified.

        :param app: The application being built
        """
        output = self.tools[app].app_context.check_output(
            [
                f"python{app.python_version_tag}",
                "-c",
                (
                    "import sys; "
                    "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                ),
            ]
        )
        target_python_tag = output.split("\n")[0]
        target_python_version = tuple(int(v) for v in target_python_tag.split("."))[:2]

        if target_python_version < self.briefcase_required_python_version:
            briefcase_min_version = ".".join(
                str(v) for v in self.briefcase_required_python_version
            )
            raise BriefcaseCommandError(
                f"The system python3 version provided by {app.target_image} "
                f"is {target_python_tag}; Briefcase requires a "
                f"minimum Python3 version of {briefcase_min_version}."
            )
        elif target_python_version != (
            self.tools.sys.version_info.major,
            self.tools.sys.version_info.minor,
        ):
            self.logger.warning(
                f"""
*************************************************************************
** WARNING: Python version mismatch!                                   **
*************************************************************************

    The system python3 provided by {app.target_image} is {target_python_tag}.
    This is not the same as your local system ({self.python_version_tag}).

    Ensure you have tested for Python version compatibility before
    releasing this app.

*************************************************************************
"""
            )

    def verify_system_python(self):
        """Verify that the Python being used to run Briefcase is the
        default system python.

        Will raise an exception if the system Python isn't an obvious
        Python3, or the Briefcase Python isn't the same version as the
        system Python.

        Requires that the app tools have been verified.
        """
        system_python_bin = Path("/usr/bin/python3").resolve()
        system_version = system_python_bin.name.split(".")
        if system_version[0] != "python3" or len(system_version) == 1:
            raise BriefcaseCommandError("Can't determine the system python version")

        if system_version[1] != str(self.tools.sys.version_info.minor):
            raise BriefcaseCommandError(
                f"The version of Python being used to run Briefcase ({self.python_version_tag}) "
                f"is not the system python3 (3.{system_version[1]})."
            )

    def verify_app_tools(self, app: AppConfig):
        """Verify App environment is prepared and available.

        When Docker is used, create or update a Docker image for the App.
        Without Docker, the host machine will be used as the App environment.

        :param app: The application being built
        """
        # Verifying the App context is idempotent; but we have some
        # additional logic that we only want to run the first time through.
        # Check (and store) the pre-verify app tool state.
        verify_python = not hasattr(self.tools[app], "app_context")

        if self.use_docker:
            DockerAppContext.verify(
                tools=self.tools,
                app=app,
                image_tag=self.docker_image_tag(app),
                dockerfile_path=self.bundle_path(app) / "Dockerfile",
                app_base_path=self.base_path,
                host_platform_path=self.platform_path,
                host_data_path=self.data_path,
                python_version=app.python_version_tag,
            )

            # Check the system Python on the target system to see if it is
            # compatible with Briefcase.
            if verify_python:
                self.verify_python(app)
        else:
            NativeAppContext.verify(tools=self.tools, app=app)

            # Check the system Python on the target system to see if it is
            # compatible with Briefcase.
            if verify_python:
                self.verify_system_python()

        # Establish Docker as app context before letting super set subprocess
        super().verify_app_tools(app)


class LinuxSystemMixin(LinuxSystemMostlyPassiveMixin):
    def verify_host(self):
        """If we're *not* using Docker, verify that we're actually on Linux."""
        super().verify_host()
        if not self.use_docker:
            if self.tools.host_os != "Linux":
                raise UnsupportedHostError(self.supported_host_os_reason)


class LinuxSystemCreateCommand(LinuxSystemMixin, LocalRequirementsMixin, CreateCommand):
    description = "Create and populate a Linux system project."

    def output_format_template_context(self, app: AppConfig):
        context = super().output_format_template_context(app)

        # The base template context includes the host Python version;
        # override that with an app-specific Python version, allowing
        # for the app to be built with the system Python.
        context["python_version"] = app.python_version_tag

        # Add the docker base image
        context["docker_base_image"] = app.target_image

        # Add the vendor base
        context["vendor_base"] = app.target_vendor_base

        return context


class LinuxSystemUpdateCommand(LinuxSystemCreateCommand, UpdateCommand):
    description = "Update an existing Linux system project."


class LinuxSystemOpenCommand(LinuxSystemMostlyPassiveMixin, DockerOpenCommand):
    description = (
        "Open a shell in a Docker container for an existing Linux system project."
    )


class LinuxSystemBuildCommand(LinuxSystemMixin, BuildCommand):
    description = "Build a Linux system project."

    def build_app(self, app: AppConfig, **kwargs):
        """Build an application.

        :param app: The application to build
        """
        self.logger.info("Building application...", prefix=app.app_name)

        self.logger.info("Build bootstrap binary...")
        with self.input.wait_bar("Building bootstrap binary..."):
            try:
                # Build the bootstrap binary.
                self.tools[app].app_context.run(
                    [
                        "make",
                        "-C",
                        "bootstrap",
                        "install",
                    ],
                    check=True,
                    cwd=self.bundle_path(app),
                )
            except subprocess.CalledProcessError as e:
                raise BriefcaseCommandError(
                    f"Error building bootstrap binary for {app.app_name}."
                ) from e

        # Make the folder for docs
        doc_folder = (
            self.bundle_path(app)
            / f"{app.app_name}-{app.version}"
            / "usr"
            / "share"
            / "doc"
            / app.app_name
        )
        doc_folder.mkdir(parents=True, exist_ok=True)

        with self.input.wait_bar("Installing license..."):
            license_file = self.base_path / "LICENSE"
            if license_file.exists():
                self.tools.shutil.copy(license_file, doc_folder / "copyright")
            else:
                raise BriefcaseCommandError(
                    """\
Your project does not contain a LICENSE file.

Create a file named `LICENSE` in the same directory as your `pyproject.toml`
with your app's licensing terms.
"""
                )

        with self.input.wait_bar("Installing changelog..."):
            changelog = self.base_path / "CHANGELOG"
            if changelog.exists():
                with changelog.open() as infile:
                    outfile = gzip.GzipFile(
                        doc_folder / "changelog.gz", mode="wb", mtime=0
                    )
                    outfile.write(infile.read().encode("utf-8"))
                    outfile.close()
            else:
                raise BriefcaseCommandError(
                    """\
Your project does not contain a CHANGELOG file.

Create a file named `CHANGELOG` in the same directory as your `pyproject.toml`
with details about the release.
"""
                )

        # Make a folder for manpages
        man_folder = (
            self.bundle_path(app)
            / f"{app.app_name}-{app.version}"
            / "usr"
            / "share"
            / "man"
            / "man1"
        )
        man_folder.mkdir(parents=True, exist_ok=True)

        with self.input.wait_bar("Installing man page..."):
            manpage_source = self.bundle_path(app) / f"{app.app_name}.1"
            if manpage_source.exists():
                with manpage_source.open() as infile:
                    outfile = gzip.GzipFile(
                        man_folder / f"{app.app_name}.1.gz", mode="wb", mtime=0
                    )
                    outfile.write(infile.read().encode("utf-8"))
                    outfile.close()
            else:
                raise BriefcaseCommandError(
                    f"Template does not provide a manpage source file `{app.app_name}.1`"
                )

        self.logger.info("Update file permissions...")
        with self.input.wait_bar("Updating file permissions..."):
            for path in self.project_path(app).glob("**/*"):
                old_perms = self.tools.os.stat(path).st_mode & 0o777
                user_perms = old_perms & 0o700
                world_perms = old_perms & 0o007

                # File permissions like 775 and 664 (where the group and user
                # permissions are the same), cause Debian heartburn. So, make
                # sure the group and world permissions are the same
                new_perms = user_perms | (world_perms << 3) | world_perms

                # If there's been any change in permissions, apply them
                if new_perms != old_perms:
                    self.logger.info(
                        "Updating file permissions on "
                        f"{path.relative_to(self.bundle_path(app))} "
                        f"from {oct(old_perms)[2:]} to {oct(new_perms)[2:]}"
                    )
                    path.chmod(new_perms)

        with self.input.wait_bar("Stripping binary..."):
            self.tools.subprocess.check_output(["strip", self.binary_path(app)])


class LinuxSystemRunCommand(LinuxSystemPassiveMixin, RunCommand):
    description = "Run a Linux system project."
    supported_host_os = {"Linux"}
    supported_host_os_reason = "Linux system projects can only be executed on Linux."

    def run_app(
        self, app: AppConfig, test_mode: bool, passthrough: List[str], **kwargs
    ):
        """Start the application.

        :param app: The config object for the app
        :param test_mode: Boolean; Is the app running in test mode?
        :param passthrough: The list of arguments to pass to the app
        """
        # Set up the log stream
        kwargs = self._prepare_app_env(app=app, test_mode=test_mode)

        # Start the app in a way that lets us stream the logs
        app_popen = self.tools.subprocess.Popen(
            [os.fsdecode(self.binary_path(app))] + passthrough,
            cwd=self.tools.home_path,
            **kwargs,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

        # Start streaming logs for the app.
        self._stream_app_logs(
            app,
            popen=app_popen,
            test_mode=test_mode,
            clean_output=False,
        )


def debian_multiline_description(description):
    """Generate a Debian multiline description string.

    The long description in a Debian control file must
    *not* contain any blank lines, and each line must start with a single space.
    Convert a long description into Debian format.

    :param description: A multi-line long description string.
    :returns: A string in Debian's multiline format
    """
    return "\n ".join(line for line in description.split("\n") if line.strip() != "")


class LinuxSystemPackageCommand(LinuxSystemMixin, PackageCommand):
    description = "Package a Linux system project."

    @property
    def packaging_formats(self):
        return ["deb", "rpm", "pkg", "system"]

    def _verify_deb_tools(self):
        """Verify that the local environment contains the debian packaging tools."""
        if not Path("/usr/bin/dpkg-deb").exists():
            raise BriefcaseCommandError(
                "Can't find the dpkg tools. Try running `sudo apt install dpkg-dev`."
            )

    def _verify_rpm_tools(self):
        """Verify that the local environment contains the redhat packaging tools."""
        if not Path("/usr/bin/rpmbuild").exists():
            raise BriefcaseCommandError(
                "Can't find the rpm-build tools. Try running `sudo dnf install rpm-build`."
            )

    def verify_app_tools(self, app):
        super().verify_app_tools(app)
        # If "system" packaging format was selected, determine what that means.
        if app.packaging_format == "system":
            app.packaging_format = {
                DEBIAN: "deb",
                RHEL: "rpm",
                ARCH: "pkg",
            }.get(app.target_vendor_base, None)

        if app.packaging_format is None:
            raise BriefcaseCommandError(
                "Briefcase doesn't know the system packaging format for "
                f"{app.target_vendor}. You may be able to build a package "
                "by manually specifying a format with -p/--packaging-format"
            )

        if not self.use_docker:
            # Check for the format-specific packaging tools.
            if app.packaging_format == "deb":
                self._verify_deb_tools()
            elif app.packaging_format == "rpm":
                self._verify_rpm_tools()

    def package_app(self, app: AppConfig, **kwargs):
        if app.packaging_format == "deb":
            self._package_deb(app, **kwargs)
        elif app.packaging_format == "rpm":
            self._package_rpm(app, **kwargs)
        else:
            raise BriefcaseCommandError(
                "Briefcase doesn't currently know how to build system packages in "
                f"{app.packaging_format.upper()} format."
            )

    def _package_deb(self, app: AppConfig, **kwargs):
        self.logger.info("Building .deb package...", prefix=app.app_name)

        # The long description *must* exist.
        if app.long_description is None:
            raise BriefcaseCommandError(
                "App configuration does not define `long_description`. "
                "Debian projects require a long description."
            )

        # Write the Debian metadata control file.
        with self.input.wait_bar("Write Debian package control file..."):
            if (self.project_path(app) / "DEBIAN").exists():
                self.tools.shutil.rmtree(self.project_path(app) / "DEBIAN")

            (self.project_path(app) / "DEBIAN").mkdir()

            # Add runtime package dependencies. App config has been finalized,
            # so this will be the target-specific definition, if one exists.
            # libc6 is added because lintian complains without it, even though
            # it's a dependency of the thing we *do* care about - python.
            system_runtime_requires = ", ".join(
                [
                    f"libc6 (>={app.glibc_version})",
                    f"python{app.python_version_tag}",
                ]
                + getattr(app, "system_runtime_requires", [])
            )

            with (self.project_path(app) / "DEBIAN" / "control").open(
                "w", encoding="utf-8"
            ) as f:
                f.write(
                    "\n".join(
                        [
                            f"Package: { app.app_name }",
                            f"Version: { app.version }",
                            f"Architecture: { self.linux_arch }",
                            f"Maintainer: { app.author } <{ app.author_email }>",
                            f"Homepage: { app.url }",
                            f"Description: { app.description }",
                            f" { debian_multiline_description(app.long_description) }",
                            f"Depends: { system_runtime_requires }",
                            f"Section: { getattr(app, 'system_section', 'utils') }",
                            "Priority: optional\n",
                        ]
                    )
                )

        with self.input.wait_bar("Building Debian package..."):
            try:
                # Build the dpkg.
                self.tools[app].app_context.run(
                    [
                        "dpkg-deb",
                        "--build",
                        "--root-owner-group",
                        f"{app.app_name}-{app.version}",
                    ],
                    check=True,
                    cwd=self.bundle_path(app),
                )
            except subprocess.CalledProcessError as e:
                raise BriefcaseCommandError(
                    f"Error while building .deb package for {app.app_name}."
                ) from e

            # Move the deb file to its final location
            self.tools.shutil.move(
                self.bundle_path(app) / f"{app.app_name}-{app.version}.deb",
                self.distribution_path(app),
            )

    def _package_rpm(self, app: AppConfig, **kwargs):
        self.logger.info("Building .rpm package...", prefix=app.app_name)

        # The long description *must* exist.
        if app.long_description is None:
            raise BriefcaseCommandError(
                "App configuration does not define `long_description`. "
                "Red Hat projects require a long description."
            )

        # Generate the rpmbuild layout
        rpmbuild_path = self.bundle_path(app) / "rpmbuild"
        with self.input.wait_bar("Generating rpmbuild layout..."):
            if rpmbuild_path.exists():
                self.tools.shutil.rmtree(rpmbuild_path)

            (rpmbuild_path / "BUILD").mkdir(parents=True)
            (rpmbuild_path / "BUILDROOT").mkdir(parents=True)
            (rpmbuild_path / "RPMS").mkdir(parents=True)
            (rpmbuild_path / "SOURCES").mkdir(parents=True)
            (rpmbuild_path / "SRPMS").mkdir(parents=True)
            (rpmbuild_path / "SPECS").mkdir(parents=True)

        # Add runtime package dependencies. App config has been finalized,
        # so this will be the target-specific definition, if one exists.
        system_runtime_requires = [
            f"python{app.python_version_tag}",
        ] + getattr(app, "system_runtime_requires", [])

        # Write the spec file
        with self.input.wait_bar("Write RPM spec file..."):
            with (rpmbuild_path / "SPECS" / f"{app.app_name}.spec").open(
                "w", encoding="utf-8"
            ) as f:
                f.write(
                    "\n".join(
                        [
                            # By default, rpmbuild thinks all .py files are executable,
                            # and if a .py doesn't have a shebang line, it will
                            # tell you that it will remove the executable bit -
                            # even if the executable bit isn't set.
                            # We disable the processor that does this.
                            "%undefine __brp_mangle_shebangs",
                            # Base package metadata
                            f"Name:           {app.app_name}",
                            f"Version:        {app.version}",
                            f"Release:        {getattr(app, 'revision', 1)}%{{?dist}}",
                            f"Summary:        {app.description}",
                            "",
                            f"License:        {getattr(app, 'license', 'Unknown')}",
                            f"URL:            {app.url}",
                            "Source0:        %{name}-%{version}.tar.gz",
                            "",
                        ]
                        + [
                            f"Requires:       {requirement}"
                            for requirement in system_runtime_requires
                        ]
                        + [
                            "",
                            f"ExclusiveArch:  {self.tools.host_arch}",
                            "",
                            "%description",
                            app.long_description,
                            "",
                            "%global debug_package %{nil}",
                            "",
                            "%prep",
                            "%autosetup",
                            "",
                            "%build",
                            "",
                            "%install",
                            "cp -r usr %{buildroot}/usr",
                        ]
                    )
                )

                f.write("\n\n%files\n")
                # Build the file manifest. Include any file that is found; also include
                # any directory that includes an app_name component, as those paths
                # will need to be cleaned up afterwards. Files that *aren't*
                # in <app_name> (sub)directories (e.g., /usr/bin/<app_name> or
                # /usr/share/man/man1/<app_name>.1.gz) will be included, but paths
                # *not* cleaned up, as they're part of more general system structures.
                for filename in sorted(self.project_path(app).glob("**/*")):
                    path = filename.relative_to(self.project_path(app))

                    if filename.is_dir():
                        if app.app_name in path.parts:
                            f.write(f"%dir /{path}\n")
                    else:
                        f.write(f"/{path}\n")

                # Add the changelog content to the bottom of the spec file.
                f.write("\n%changelog\n")
                changelog_source = self.base_path / "CHANGELOG"
                if not changelog_source.exists():
                    raise BriefcaseCommandError(
                        """\
Your project does not contain a CHANGELOG file.

Create a file named `CHANGELOG` in the same directory as your `pyproject.toml`
with details about the release.
"""
                    )
                with changelog_source.open(encoding="utf-8") as c:
                    f.write(c.read())

        with self.input.wait_bar("Building source archive..."):
            self.tools.shutil.make_archive(
                rpmbuild_path / "SOURCES" / f"{app.app_name}-{app.version}",
                format="gztar",
                root_dir=self.bundle_path(app),
                base_dir=f"{app.app_name}-{app.version}",
            )

        with self.input.wait_bar("Building RPM package..."):
            try:
                # Build the dpkg.
                self.tools[app].app_context.run(
                    [
                        "rpmbuild",
                        "-bb",
                        "--define",
                        f"_topdir {self.bundle_path(app) / 'rpmbuild'}",
                        f"./rpmbuild/SPECS/{app.app_name}.spec",
                    ],
                    check=True,
                    cwd=self.bundle_path(app),
                )
            except subprocess.CalledProcessError as e:
                raise BriefcaseCommandError(
                    f"Error while building .rpm package for {app.app_name}."
                ) from e

        # Move the rpm file to it's final location
        self.tools.shutil.move(
            rpmbuild_path
            / "RPMS"
            / self.tools.host_arch
            / self.distribution_filename(app),
            self.distribution_path(app),
        )


class LinuxSystemPublishCommand(LinuxSystemMixin, PublishCommand):
    description = "Publish a Linux system project."


# Declare the briefcase command bindings
create = LinuxSystemCreateCommand  # noqa
update = LinuxSystemUpdateCommand  # noqa
open = LinuxSystemOpenCommand  # noqa
build = LinuxSystemBuildCommand  # noqa
run = LinuxSystemRunCommand  # noqa
package = LinuxSystemPackageCommand  # noqa
publish = LinuxSystemPublishCommand  # noqa
