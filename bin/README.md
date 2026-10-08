# Runtime binaries

The source repository intentionally does not track the portable runtime payload stored in this directory.

Official packaged releases populate `bin/` with the runtime components required by that release. These can include the embedded Python environment, media tools, NVIDIA GPU runtimes, project-owned native components, native workers, third-party libraries, license materials, and other release-only dependencies.

Many of these files are large, generated, prebuilt, proprietary, or distributed under separate third-party licenses. They are therefore shipped with official packaged releases rather than committed to the source repository.

## Source checkout

A normal Git clone or GitHub-generated **Source code** archive is not the complete ready-to-run portable application because it does not contain the release runtime payload under `bin/`.

Use an official packaged release when you need the complete application.

## Development and packaging

Local development and packaging may populate `bin/` with the runtime files required by the current application version.

Everything in this directory is ignored by Git except this `README.md` and `.gitignore`.

Runtime locations and required files are defined and validated by the application itself. This document intentionally does not duplicate binary versions, hashes, exact filenames, or release-specific folder layouts so that normal runtime and dependency updates do not require changes to this file.

## Licensing

Project-owned Visual Enhancer material, including applicable source code, executable code, native components, documentation, and other materials owned by Merserk, is governed by the [Merserk Source License 1.0](../LICENSE).

## Third-party components

Third-party software, SDKs, runtimes, libraries, codecs, tools, and other components retain their own copyrights, licenses, redistribution requirements, and notices. They are not relicensed under the Merserk Source License merely because they are included with Visual Enhancer.

See [THIRD-PARTY-NOTICES](../THIRD-PARTY-NOTICES) for the project's third-party attribution and licensing information.
