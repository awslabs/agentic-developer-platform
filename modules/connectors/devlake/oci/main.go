package main

import (
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/google/go-containerregistry/pkg/crane"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/google/go-containerregistry/pkg/v1/validate"
)

func build(baseRef, platform, layerPath, output, revision, component string) error {
	segments := strings.Split(platform, "/")
	if len(segments) != 2 || segments[0] != "linux" || (segments[1] != "amd64" && segments[1] != "arm64") {
		return fmt.Errorf("unsupported platform %q", platform)
	}
	base, err := crane.Pull(baseRef, crane.WithPlatform(&v1.Platform{OS: segments[0], Architecture: segments[1]}))
	if err != nil {
		return err
	}
	config, err := base.ConfigFile()
	if err != nil {
		return err
	}
	if config.OS != segments[0] || config.Architecture != segments[1] {
		return fmt.Errorf("base architecture differs from source lock")
	}
	layer, err := tarball.LayerFromFile(layerPath, tarball.WithMediaType(types.OCILayer))
	if err != nil {
		return err
	}
	image, err := mutate.Append(base, mutate.Addendum{Layer: layer, Annotations: map[string]string{"org.opencontainers.image.title": "devlake-" + component}})
	if err != nil {
		return err
	}
	config, err = image.ConfigFile()
	if err != nil {
		return err
	}
	config = config.DeepCopy()
	if config.Config.Labels == nil {
		config.Config.Labels = map[string]string{}
	}
	config.Config.Labels["org.opencontainers.image.revision"] = revision
	config.Config.Labels["org.opencontainers.image.title"] = "devlake-" + component
	config.Config.User = "65532:65532"
	config.Config.WorkingDir = "/"
	config.Config.Cmd = nil
	config.Config.Entrypoint = []string{"/usr/local/bin/" + map[string]string{"api": "connector-api", "worker": "collector-worker"}[component]}
	image, err = mutate.ConfigFile(image, config)
	if err != nil {
		return err
	}
	image = mutate.MediaType(image, types.OCIManifestSchema1)
	image = mutate.ConfigMediaType(image, types.OCIConfigJSON)
	if err = validate.Image(image); err != nil {
		return err
	}
	if err = crane.SaveOCI(image, output); err != nil {
		return err
	}
	digest, err := image.Digest()
	if err != nil {
		return err
	}
	fmt.Println(digest)
	return nil
}

func main() {
	base := flag.String("base", "", "digest-qualified runtime base image")
	platform := flag.String("platform", "", "target operating system and architecture")
	layer := flag.String("layer", "", "component tar layer")
	output := flag.String("output", "", "new OCI image-layout directory")
	revision := flag.String("revision", "", "pinned customization commit")
	component := flag.String("component", "", "api or worker")
	flag.Parse()
	if *base == "" || *layer == "" || *output == "" || *revision == "" || (*component != "api" && *component != "worker") {
		fmt.Fprintln(os.Stderr, "base, layer, output, revision and valid component are required")
		os.Exit(2)
	}
	if err := build(*base, *platform, *layer, *output, *revision, *component); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
