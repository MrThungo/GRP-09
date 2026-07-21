using System;
using System.Collections.Specialized;
using System.Configuration;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Web;

public class PythonBridgeHandler : IHttpHandler
{
    private static readonly object SyncRoot = new object();
    private static Process pythonProcess;
    private static int selectedPort;
    private static string deploymentId;

    public bool IsReusable
    {
        get { return true; }
    }

    public void ProcessRequest(HttpContext context)
    {
        try
        {
            if (TryServeStaticFile(context))
            {
                return;
            }

            EnsurePythonServer(context);
            ProxyRequest(context);
        }
        catch (Exception ex)
        {
            WriteBridgeLog("bridge_error " + ex.GetType().FullName + ": " + ex.Message);
            context.Response.StatusCode = 503;
            context.Response.ContentType = "text/plain";
            context.Response.Write("The application is starting or temporarily unavailable.\n");
            context.Response.Write(ex.GetType().FullName + ": " + ex.Message + "\n");
        }
    }

    private static void EnsurePythonServer(HttpContext context)
    {
        if (selectedPort > 0 && CheckPythonHealth(selectedPort))
        {
            return;
        }

        lock (SyncRoot)
        {
            if (selectedPort > 0 && CheckPythonHealth(selectedPort))
            {
                return;
            }

            StopTrackedPythonProcess();
            StopPidFileProcess();

            selectedPort = ResolveBridgePort();
            StartPythonProcess(context, selectedPort);
            WaitForPython(selectedPort);
        }
    }

    private static void StartPythonProcess(HttpContext context, int port)
    {
        string root = AppRoot;
        string pythonPath = Setting("PYTHON_BRIDGE_PYTHON", Path.Combine(root, ".python", "python.exe"));
        string appScript = Setting("PYTHON_BRIDGE_SCRIPT", Path.Combine(root, "wsgi.py"));

        var startInfo = new ProcessStartInfo(pythonPath, Quote(appScript));
        startInfo.WorkingDirectory = root;
        startInfo.UseShellExecute = false;
        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;
        startInfo.CreateNoWindow = true;

        ApplyEnvironment(startInfo.EnvironmentVariables, context, port);

        pythonProcess = new Process();
        pythonProcess.StartInfo = startInfo;
        pythonProcess.EnableRaisingEvents = true;
        pythonProcess.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
        {
            WriteBridgeLog(args.Data);
        };
        pythonProcess.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
        {
            WriteBridgeLog(args.Data);
        };

        pythonProcess.Start();
        pythonProcess.BeginOutputReadLine();
        pythonProcess.BeginErrorReadLine();
        WritePidFile(pythonProcess.Id);
        WriteBridgeLog("started_python pid=" + pythonProcess.Id + " port=" + port + " deploy=" + DeploymentId);
    }

    private static void ApplyEnvironment(StringDictionary environment, HttpContext context, int port)
    {
        string appPath = context.Request.ApplicationPath;
        if (String.IsNullOrEmpty(appPath) || appPath == "/")
        {
            appPath = Setting("PYTHON_BRIDGE_BASE_PATH", "");
        }

        environment["HOST"] = "127.0.0.1";
        environment["PORT"] = port.ToString(CultureInfo.InvariantCulture);
        environment["PYTHON_BRIDGE_DEPLOYMENT_ID"] = DeploymentId;
        environment["FLASK_DEBUG"] = Setting("FLASK_DEBUG", "0");
        environment["FLASK_ENV"] = Setting("FLASK_ENV", "production");
        environment["SESSION_COOKIE_SECURE"] = Setting("SESSION_COOKIE_SECURE", "true");
        environment["PREFERRED_URL_SCHEME"] = Setting("PREFERRED_URL_SCHEME", "https");
        environment["TRUST_PROXY_HEADERS"] = Setting("TRUST_PROXY_HEADERS", "true");
        environment["PROXY_FIX_X_FOR"] = Setting("PROXY_FIX_X_FOR", "1");
        environment["PROXY_FIX_X_PROTO"] = Setting("PROXY_FIX_X_PROTO", "1");
        environment["PROXY_FIX_X_HOST"] = Setting("PROXY_FIX_X_HOST", "1");
        environment["PROXY_FIX_X_PREFIX"] = Setting("PROXY_FIX_X_PREFIX", "1");
        environment["ENABLE_QUICK_LOGIN"] = Setting("ENABLE_QUICK_LOGIN", "true");
        environment["APP_BASE_URL"] = Setting(
            "APP_BASE_URL",
            context.Request.Url.GetLeftPart(UriPartial.Authority) + appPath
        );
        environment["PYTHONUNBUFFERED"] = "1";
        environment["PYTHONDONTWRITEBYTECODE"] = "1";
        environment["PYTHONIOENCODING"] = "utf-8";
    }

    private static void WaitForPython(int port)
    {
        int timeoutSeconds = IntSetting("PYTHON_BRIDGE_STARTUP_TIMEOUT_SECONDS", 60);
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);

        while (DateTime.UtcNow < deadline)
        {
            if (pythonProcess != null && pythonProcess.HasExited)
            {
                throw new InvalidOperationException("Python exited during startup with code " + pythonProcess.ExitCode + ".");
            }
            if (CheckPythonHealth(port))
            {
                return;
            }
            Thread.Sleep(500);
        }

        throw new TimeoutException("Python did not become healthy on 127.0.0.1:" + port + ".");
    }

    private static bool CheckPythonHealth(int port)
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/__bridge/health");
            request.Method = "GET";
            request.Timeout = 3000;
            request.ReadWriteTimeout = 3000;
            request.Proxy = null;

            using (var response = (HttpWebResponse)request.GetResponse())
            using (var stream = response.GetResponseStream())
            using (var reader = new StreamReader(stream))
            {
                string body = reader.ReadToEnd();
                return response.StatusCode == HttpStatusCode.OK
                    && body.IndexOf(DeploymentId, StringComparison.Ordinal) >= 0;
            }
        }
        catch
        {
            return false;
        }
    }

    private static void ProxyRequest(HttpContext context)
    {
        HttpRequest incoming = context.Request;
        string targetUrl = "http://127.0.0.1:" + selectedPort + BuildBackendPath(incoming);
        var backend = (HttpWebRequest)WebRequest.Create(targetUrl);

        backend.Method = incoming.HttpMethod;
        backend.AllowAutoRedirect = false;
        int proxyTimeout = IntSetting("PYTHON_BRIDGE_PROXY_TIMEOUT_SECONDS", 120) * 1000;
        backend.Timeout = proxyTimeout;
        backend.ReadWriteTimeout = proxyTimeout;
        backend.Proxy = null;

        CopyRequestHeaders(incoming, backend);
        CopyRequestBody(incoming, backend);

        try
        {
            using (var backendResponse = (HttpWebResponse)backend.GetResponse())
            {
                CopyResponse(context.Response, backendResponse);
            }
        }
        catch (WebException ex)
        {
            var errorResponse = ex.Response as HttpWebResponse;
            if (errorResponse == null)
            {
                throw;
            }
            using (errorResponse)
            {
                CopyResponse(context.Response, errorResponse);
            }
        }
    }

    private static bool TryServeStaticFile(HttpContext context)
    {
        HttpRequest request = context.Request;
        string appPath = GetApplicationRelativePath(request, false);
        if (!appPath.StartsWith("/static/", StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        HttpResponse response = context.Response;
        if (request.HttpMethod != "GET" && request.HttpMethod != "HEAD")
        {
            response.StatusCode = 405;
            response.AddHeader("Allow", "GET, HEAD");
            return true;
        }

        string relative = Uri.UnescapeDataString(appPath.Substring("/static/".Length)).Replace('/', Path.DirectorySeparatorChar);
        string staticRoot = Path.GetFullPath(Path.Combine(AppRoot, "app", "static"));
        string filePath = Path.GetFullPath(Path.Combine(staticRoot, relative));

        if (!filePath.StartsWith(staticRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            response.StatusCode = 404;
            return true;
        }
        if (!File.Exists(filePath))
        {
            response.StatusCode = 404;
            return true;
        }

        ServeFile(context, filePath);
        return true;
    }

    private static void ServeFile(HttpContext context, string filePath)
    {
        HttpRequest request = context.Request;
        HttpResponse response = context.Response;
        var file = new FileInfo(filePath);
        long fileLength = file.Length;

        response.ContentType = MimeType(file.Extension);
        response.Cache.SetCacheability(HttpCacheability.Public);
        response.Cache.SetMaxAge(TimeSpan.FromDays(7));
        response.Cache.SetLastModified(file.LastWriteTimeUtc);
        response.AddHeader("Accept-Ranges", "bytes");

        string rangeHeader = request.Headers["Range"];
        if (!String.IsNullOrWhiteSpace(rangeHeader) && rangeHeader.StartsWith("bytes=", StringComparison.OrdinalIgnoreCase))
        {
            long start;
            long end;
            if (!TryParseRange(rangeHeader, fileLength, out start, out end))
            {
                response.StatusCode = 416;
                response.AddHeader("Content-Range", "bytes */" + fileLength);
                return;
            }

            long rangeLength = end - start + 1;
            response.StatusCode = 206;
            response.AddHeader("Content-Range", "bytes " + start + "-" + end + "/" + fileLength);
            response.AddHeader("Content-Length", rangeLength.ToString(CultureInfo.InvariantCulture));
            if (request.HttpMethod != "HEAD")
            {
                response.TransmitFile(filePath, start, rangeLength);
            }
            return;
        }

        response.StatusCode = 200;
        response.AddHeader("Content-Length", fileLength.ToString(CultureInfo.InvariantCulture));
        if (request.HttpMethod != "HEAD")
        {
            response.TransmitFile(filePath);
        }
    }

    private static bool TryParseRange(string rangeHeader, long fileLength, out long start, out long end)
    {
        start = 0;
        end = fileLength - 1;
        string range = rangeHeader.Substring("bytes=".Length).Split(',')[0].Trim();
        string[] parts = range.Split('-');
        if (parts.Length != 2)
        {
            return false;
        }

        if (String.IsNullOrWhiteSpace(parts[0]))
        {
            long suffixLength;
            if (!Int64.TryParse(parts[1], out suffixLength) || suffixLength <= 0)
            {
                return false;
            }
            start = Math.Max(0, fileLength - suffixLength);
            end = fileLength - 1;
        }
        else
        {
            if (!Int64.TryParse(parts[0], out start))
            {
                return false;
            }
            if (!String.IsNullOrWhiteSpace(parts[1]) && !Int64.TryParse(parts[1], out end))
            {
                return false;
            }
            if (String.IsNullOrWhiteSpace(parts[1]))
            {
                end = fileLength - 1;
            }
        }

        return fileLength > 0 && start >= 0 && end >= start && start < fileLength;
    }

    private static string BuildBackendPath(HttpRequest incoming)
    {
        string rawUrl = incoming.RawUrl;
        string appPath = incoming.ApplicationPath;

        if (!String.IsNullOrEmpty(appPath) && appPath != "/" && rawUrl.StartsWith(appPath, StringComparison.OrdinalIgnoreCase))
        {
            rawUrl = rawUrl.Substring(appPath.Length);
        }

        return String.IsNullOrEmpty(rawUrl) ? "/" : rawUrl;
    }

    private static string GetApplicationRelativePath(HttpRequest request, bool includeQuery)
    {
        string value = includeQuery ? request.RawUrl : request.Url.AbsolutePath;
        string appPath = request.ApplicationPath;
        if (!String.IsNullOrEmpty(appPath) && appPath != "/" && value.StartsWith(appPath, StringComparison.OrdinalIgnoreCase))
        {
            value = value.Substring(appPath.Length);
        }
        return String.IsNullOrEmpty(value) ? "/" : value;
    }

    private static void CopyRequestHeaders(HttpRequest incoming, HttpWebRequest backend)
    {
        if (!String.IsNullOrEmpty(incoming.UserAgent))
        {
            backend.UserAgent = incoming.UserAgent;
        }
        if (!String.IsNullOrEmpty(incoming.Headers["Accept"]))
        {
            backend.Accept = incoming.Headers["Accept"];
        }
        if (!String.IsNullOrEmpty(incoming.ContentType))
        {
            backend.ContentType = incoming.ContentType;
        }
        if (incoming.UrlReferrer != null)
        {
            backend.Referer = incoming.UrlReferrer.ToString();
        }
        if (!String.IsNullOrEmpty(incoming.Headers["Host"]))
        {
            backend.Host = incoming.Headers["Host"];
        }

        string forwardedFor = incoming.UserHostAddress;
        if (!String.IsNullOrWhiteSpace(incoming.Headers["X-Forwarded-For"]))
        {
            forwardedFor = incoming.Headers["X-Forwarded-For"] + ", " + forwardedFor;
        }

        backend.Headers["X-Forwarded-For"] = forwardedFor;
        backend.Headers["X-Forwarded-Proto"] = incoming.IsSecureConnection ? "https" : "http";
        backend.Headers["X-Forwarded-Host"] = incoming.Headers["Host"];
        if (!String.IsNullOrEmpty(incoming.ApplicationPath) && incoming.ApplicationPath != "/")
        {
            backend.Headers["X-Forwarded-Prefix"] = incoming.ApplicationPath;
        }

        foreach (string key in incoming.Headers.AllKeys)
        {
            if (String.IsNullOrEmpty(key) || IsRestrictedRequestHeader(key))
            {
                continue;
            }
            backend.Headers[key] = incoming.Headers[key];
        }
    }

    private static void CopyRequestBody(HttpRequest incoming, HttpWebRequest backend)
    {
        if (incoming.HttpMethod == "GET" || incoming.HttpMethod == "HEAD" || incoming.InputStream == null)
        {
            return;
        }

        if (incoming.ContentLength > 0)
        {
            backend.ContentLength = incoming.ContentLength;
        }
        else
        {
            backend.SendChunked = true;
        }

        using (Stream backendStream = backend.GetRequestStream())
        {
            incoming.InputStream.CopyTo(backendStream);
        }
    }

    private static void CopyResponse(HttpResponse outgoing, HttpWebResponse backendResponse)
    {
        outgoing.StatusCode = (int)backendResponse.StatusCode;
        outgoing.StatusDescription = backendResponse.StatusDescription;
        outgoing.ContentType = backendResponse.ContentType;

        foreach (string key in backendResponse.Headers.AllKeys)
        {
            if (String.IsNullOrEmpty(key) || IsRestrictedResponseHeader(key))
            {
                continue;
            }
            string[] values = backendResponse.Headers.GetValues(key);
            if (values == null)
            {
                continue;
            }
            foreach (string value in values)
            {
                outgoing.AppendHeader(key, value);
            }
        }

        using (Stream responseStream = backendResponse.GetResponseStream())
        {
            if (responseStream != null)
            {
                responseStream.CopyTo(outgoing.OutputStream);
            }
        }
    }

    private static int ResolveBridgePort()
    {
        int configured = IntSetting("PYTHON_BRIDGE_PORT", 0);
        return configured > 0 ? configured : FindFreePort();
    }

    private static int FindFreePort()
    {
        TcpListener listener = null;
        try
        {
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            if (listener != null)
            {
                listener.Stop();
            }
        }
    }

    private static void StopTrackedPythonProcess()
    {
        if (pythonProcess == null)
        {
            return;
        }
        try
        {
            if (!pythonProcess.HasExited)
            {
                pythonProcess.Kill();
                pythonProcess.WaitForExit(5000);
            }
        }
        catch
        {
        }
        pythonProcess = null;
    }

    private static void StopPidFileProcess()
    {
        string path = PidPath;
        if (!File.Exists(path))
        {
            return;
        }

        try
        {
            int pid;
            if (Int32.TryParse(File.ReadAllText(path).Trim(), out pid))
            {
                Process process = Process.GetProcessById(pid);
                if (process.ProcessName.IndexOf("python", StringComparison.OrdinalIgnoreCase) >= 0 && !process.HasExited)
                {
                    process.Kill();
                    process.WaitForExit(5000);
                    WriteBridgeLog("stopped_stale_python pid=" + pid);
                }
            }
        }
        catch
        {
        }

        try
        {
            File.Delete(path);
        }
        catch
        {
        }
    }

    private static void WritePidFile(int pid)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(PidPath));
            File.WriteAllText(PidPath, pid.ToString(CultureInfo.InvariantCulture));
        }
        catch
        {
        }
    }

    private static void WriteBridgeLog(string line)
    {
        if (String.IsNullOrEmpty(line))
        {
            return;
        }

        try
        {
            string logDirectory = Path.Combine(AppRoot, "App_Data", "logs");
            Directory.CreateDirectory(logDirectory);
            File.AppendAllText(
                Path.Combine(logDirectory, "bridge-python.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + line + Environment.NewLine
            );
        }
        catch
        {
        }
    }

    private static string MimeType(string extension)
    {
        switch ((extension ?? "").ToLowerInvariant())
        {
            case ".css": return "text/css; charset=utf-8";
            case ".js": return "application/javascript; charset=utf-8";
            case ".json": return "application/json; charset=utf-8";
            case ".svg": return "image/svg+xml";
            case ".png": return "image/png";
            case ".jpg":
            case ".jpeg": return "image/jpeg";
            case ".gif": return "image/gif";
            case ".webp": return "image/webp";
            case ".ico": return "image/x-icon";
            case ".mp4": return "video/mp4";
            case ".webm": return "video/webm";
            case ".ogg": return "video/ogg";
            case ".woff": return "font/woff";
            case ".woff2": return "font/woff2";
            case ".ttf": return "font/ttf";
            default: return "application/octet-stream";
        }
    }

    private static string AppRoot
    {
        get { return HttpRuntime.AppDomainAppPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar); }
    }

    private static string PidPath
    {
        get { return Path.Combine(AppRoot, "App_Data", "python-bridge.pid"); }
    }

    private static string DeploymentId
    {
        get
        {
            if (String.IsNullOrEmpty(deploymentId))
            {
                deploymentId = Setting("PYTHON_BRIDGE_DEPLOYMENT_ID", "");
                if (String.IsNullOrEmpty(deploymentId))
                {
                    string configPath = Path.Combine(AppRoot, "web.config");
                    deploymentId = File.Exists(configPath)
                        ? File.GetLastWriteTimeUtc(configPath).Ticks.ToString(CultureInfo.InvariantCulture)
                        : DateTime.UtcNow.Ticks.ToString(CultureInfo.InvariantCulture);
                }
            }
            return deploymentId;
        }
    }

    private static string Setting(string key, string fallback)
    {
        string value = ConfigurationManager.AppSettings[key];
        return String.IsNullOrWhiteSpace(value) ? fallback : value;
    }

    private static int IntSetting(string key, int fallback)
    {
        int value;
        return Int32.TryParse(ConfigurationManager.AppSettings[key], out value) ? value : fallback;
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static bool IsRestrictedRequestHeader(string key)
    {
        string lower = key.ToLowerInvariant();
        return lower == "accept"
            || lower == "connection"
            || lower == "content-length"
            || lower == "content-type"
            || lower == "expect"
            || lower == "host"
            || lower == "if-modified-since"
            || lower == "referer"
            || lower == "transfer-encoding"
            || lower == "user-agent"
            || lower == "proxy-connection";
    }

    private static bool IsRestrictedResponseHeader(string key)
    {
        string lower = key.ToLowerInvariant();
        return lower == "connection"
            || lower == "content-length"
            || lower == "keep-alive"
            || lower == "proxy-authenticate"
            || lower == "proxy-authorization"
            || lower == "te"
            || lower == "trailer"
            || lower == "transfer-encoding"
            || lower == "upgrade";
    }
}
