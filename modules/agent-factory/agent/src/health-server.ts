/**
 * Simple health server for Kubernetes probes
 * Runs alongside the main agent application
 */
import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';

interface HealthStatus {
  status: 'healthy' | 'unhealthy';
  timestamp: string;
  uptime: number;
  checks: {
    filesystem: boolean;
    memory: boolean;
  };
}

class HealthServer {
  private server: http.Server;
  private startTime: number;

  constructor(private port: number = 8765) {
    this.startTime = Date.now();
    this.server = this.createServer();
  }

  private createServer(): http.Server {
    return http.createServer((req, res) => {
      // CORS headers
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

      if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
      }

      if (req.method === 'GET') {
        switch (req.url) {
          case '/health':
            this.handleHealthCheck(res);
            break;
          case '/ready':
            this.handleReadinessCheck(res);
            break;
          case '/startup':
            this.handleStartupCheck(res);
            break;
          default:
            this.handle404(res);
            break;
        }
      } else {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Method Not Allowed' }));
      }
    });
  }

  private async handleHealthCheck(res: http.ServerResponse): Promise<void> {
    try {
      const status = await this.performHealthChecks();
      const httpStatus = status.status === 'healthy' ? 200 : 503;

      res.writeHead(httpStatus, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(status));
    } catch (error) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'unhealthy',
        timestamp: new Date().toISOString(),
        error: error instanceof Error ? error.message : 'Unknown error'
      }));
    }
  }

  private handleReadinessCheck(res: http.ServerResponse): void {
    // Simple readiness check - just return 200 if server is running
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ready',
      timestamp: new Date().toISOString()
    }));
  }

  private handleStartupCheck(res: http.ServerResponse): void {
    // Check if the application has been running for at least 10 seconds
    const uptimeSeconds = (Date.now() - this.startTime) / 1000;
    const isStarted = uptimeSeconds > 10;

    const httpStatus = isStarted ? 200 : 503;
    res.writeHead(httpStatus, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: isStarted ? 'started' : 'starting',
      timestamp: new Date().toISOString(),
      uptime: uptimeSeconds
    }));
  }

  private handle404(res: http.ServerResponse): void {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      error: 'Not Found',
      availableEndpoints: ['/health', '/ready', '/startup']
    }));
  }

  private async performHealthChecks(): Promise<HealthStatus> {
    const uptime = (Date.now() - this.startTime) / 1000;

    // Check filesystem access
    const filesystemCheck = await this.checkFilesystem();

    // Check memory usage
    const memoryCheck = this.checkMemory();

    const allChecksPass = filesystemCheck && memoryCheck;

    return {
      status: allChecksPass ? 'healthy' : 'unhealthy',
      timestamp: new Date().toISOString(),
      uptime,
      checks: {
        filesystem: filesystemCheck,
        memory: memoryCheck
      }
    };
  }

  private async checkFilesystem(): Promise<boolean> {
    try {
      // Check if /data directory is accessible
      await fs.promises.access('/data', fs.constants.W_OK);
      return true;
    } catch {
      return false;
    }
  }

  private checkMemory(): boolean {
    try {
      const usage = process.memoryUsage();
      const heapUsedMB = usage.heapUsed / 1024 / 1024;
      // Flag as unhealthy if using more than 1GB heap
      return heapUsedMB < 1024;
    } catch {
      return false;
    }
  }

  public start(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.server.listen(this.port, (err?: Error) => {
        if (err) {
          reject(err);
        } else {
          console.log(`Health server listening on port ${this.port}`);
          resolve();
        }
      });
    });
  }

  public stop(): Promise<void> {
    return new Promise((resolve) => {
      this.server.close(() => {
        console.log('Health server stopped');
        resolve();
      });
    });
  }
}

// If running directly (not imported), start the server
if (require.main === module) {
  const port = parseInt(process.env.PORT || '8765', 10);
  const healthServer = new HealthServer(port);

  healthServer.start().catch(console.error);

  // Graceful shutdown
  process.on('SIGTERM', async () => {
    console.log('Received SIGTERM, shutting down health server');
    await healthServer.stop();
    process.exit(0);
  });

  process.on('SIGINT', async () => {
    console.log('Received SIGINT, shutting down health server');
    await healthServer.stop();
    process.exit(0);
  });
}

export { HealthServer };