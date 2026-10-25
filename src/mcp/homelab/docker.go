package main

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
)

type DockerClient interface {
	ListContainers(ctx context.Context) (string, error)
	GetContainerLogs(ctx context.Context, containerID string) (string, error)
}

type SSHDockerClient struct {
	Host string
	User string
}

func (c *SSHDockerClient) runSSH(ctx context.Context, args ...string) (string, error) {
	sshArgs := []string{fmt.Sprintf("%s@%s", c.User, c.Host)}
	sshArgs = append(sshArgs, args...)

	// #nosec G204
	cmd := exec.CommandContext(ctx, "ssh", sshArgs...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("ssh error: %v, stderr: %s", err, stderr.String())
	}
	return stdout.String(), nil
}

func (c *SSHDockerClient) ListContainers(ctx context.Context) (string, error) {
	return c.runSSH(ctx, "docker", "ps", "-a", "--format", "{{.ID}}\\t{{.Names}}\\t{{.Status}}")
}

func (c *SSHDockerClient) GetContainerLogs(ctx context.Context, containerID string) (string, error) {
	if containerID == "" {
		return "", fmt.Errorf("containerID cannot be empty")
	}
	return c.runSSH(ctx, "docker", "logs", "--tail", "100", containerID)
}
