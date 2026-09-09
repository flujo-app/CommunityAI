// Standalone .NET Framework 4 downloader. Never launches the downloaded file.
// CLI: URL output size sha256 progress cancel parentPid parentCreationFileTime
// Atomic progress: downloaded|total|status (0 download, 1 retry, 2 hash,
// 3 verified, 4 failed, 5 cancelled). Only exit 0 means verified success.
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

internal static class WindowsDownload
{
    private const int Attempts = 8;
#if WINDOWS_DOWNLOAD_TEST
    private const int IoTimeout = 500;
    private const int BackoffBase = 20;
#else
    private const int IoTimeout = 30000;
    private const int BackoffBase = 1000;
#endif
#if WINDOWS_DOWNLOAD_SHORT_DEADLINE && WINDOWS_DOWNLOAD_TEST
    private const long DeadlineMilliseconds = 700;
#else
    private const long DeadlineMilliseconds = 2L * 60 * 60 * 1000;
#endif
    private static readonly Stopwatch Clock = new Stopwatch();
    private static readonly ManualResetEvent Finished = new ManualResetEvent(false);
    private static readonly ManualResetEvent Cancelled = new ManualResetEvent(false);
    private static readonly object RequestLock = new object();
    private static HttpWebRequest CurrentRequest;
    private static Process Parent;
    private static IntPtr ParentHandle;
    private static string OutputPath, ProgressPath, CancelPath;
    private static long ExpectedSize, Downloaded, LastProgress = -1000;
    private static int StopReason;
    private static bool OwnsOutput;
    private static string FailureDetail = "Download failed";
    private static string LastTransferError = "Incomplete response";

    [DllImport("kernel32.dll", ExactSpelling = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    private sealed class InvalidDownload : Exception
    {
        internal InvalidDownload(string message) : base(message) { }
    }

    private sealed class Stopped : Exception { }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidDownload(message);
    }

    private static void CheckStop()
    {
        if (Clock.ElapsedMilliseconds >= DeadlineMilliseconds)
            Interlocked.CompareExchange(ref StopReason, 3, 0);
        if (Interlocked.CompareExchange(ref StopReason, 0, 0) != 0) throw new Stopped();
    }

    private static void Watch()
    {
        while (!Finished.WaitOne(100))
        {
            int reason = 0;
            try
            {
                if (File.Exists(CancelPath)) reason = 1;
                // A failed parent-handle query also stops the transfer.
                else if (WaitForSingleObject(ParentHandle, 0) != 258) reason = 2;
                else if (Clock.ElapsedMilliseconds >= DeadlineMilliseconds) reason = 3;
            }
            catch { reason = 2; }
            if (reason == 0) continue;
            Interlocked.CompareExchange(ref StopReason, reason, 0);
            Cancelled.Set();
            lock (RequestLock)
            {
                if (CurrentRequest != null)
                {
                    try { CurrentRequest.Abort(); } catch { }
                }
            }
            return;
        }
    }

    private static void Progress(int status, bool final)
    {
        long remaining = 250 - (Clock.ElapsedMilliseconds - LastProgress);
        if (remaining > 0)
        {
            if (!final) return;
            Thread.Sleep((int)remaining);
        }
        string temporary = ProgressPath + ".new";
        bool ownsTemporary = false;
        try
        {
            using (FileStream stream = new FileStream(temporary, FileMode.CreateNew,
                                                      FileAccess.Write, FileShare.None))
            {
                ownsTemporary = true;
                byte[] bytes = Encoding.ASCII.GetBytes(
                    Downloaded.ToString(CultureInfo.InvariantCulture) + "|" +
                    ExpectedSize.ToString(CultureInfo.InvariantCulture) + "|" +
                    status.ToString(CultureInfo.InvariantCulture));
                stream.Write(bytes, 0, bytes.Length);
            }
            Stopwatch publication = Stopwatch.StartNew();
            while (true)
            {
                try
                {
                    if (File.Exists(ProgressPath)) File.Replace(temporary, ProgressPath, null);
                    else File.Move(temporary, ProgressPath);
                    break;
                }
                catch (IOException error)
                {
                    int code = Marshal.GetHRForException(error) & 0xffff;
                    if ((code != 32 && code != 33) || publication.ElapsedMilliseconds >= 75) throw;
                    // Inno's progress reader may briefly deny delete sharing.
                    if (!final) CheckStop();
                    Thread.Sleep(25);
                }
            }
            ownsTemporary = false;
            LastProgress = Clock.ElapsedMilliseconds;
        }
        catch (Stopped) { throw; }
        // Presentation never authorizes execution: Run independently verifies
        // the complete size/hash, and Inno checks both again after exit 0.
        catch (IOException error) { ProgressWarning(error); }
        catch (UnauthorizedAccessException error) { ProgressWarning(error); }
        catch (SecurityException error) { ProgressWarning(error); }
        finally
        {
            if (ownsTemporary) { try { File.Delete(temporary); } catch { } }
            LastProgress = Clock.ElapsedMilliseconds;
        }
    }

    private static void ProgressWarning(Exception error)
    {
        try
        {
            // No path, URL, stack or response headers are written here.
            string warning = "Skipped progress frame: " + error.GetType().Name +
                             " HRESULT " + Marshal.GetHRForException(error).ToString("X8", CultureInfo.InvariantCulture);
            File.WriteAllText(ProgressPath + ".warning", warning, new UTF8Encoding(false));
        }
        catch { }
    }

    private static Uri ValidateUrl(string text)
    {
        Uri uri;
        Require(text.Length <= 2048 && Uri.TryCreate(text, UriKind.Absolute, out uri), "Invalid URL");
        uri = new Uri(text, UriKind.Absolute);
        bool allowed = uri.Scheme == "https" && uri.Port == 443;
#if WINDOWS_DOWNLOAD_TEST
        allowed = allowed || (uri.Scheme == "http" && uri.Host == "127.0.0.1");
#endif
        Require(allowed && uri.UserInfo.Length == 0 && uri.Query.Length == 0 &&
                uri.Fragment.Length == 0 && !String.IsNullOrEmpty(uri.Host), "Invalid HTTPS URL");
        return uri;
    }

    private static void ValidateRangeHeader(string header, long offset, long expectedSize)
    {
        Match match = Regex.Match(header ?? "", @"\Abytes ([0-9]+)-([0-9]+)/([0-9]+)\z");
        long first, last, total;
        Require(match.Success &&
                Int64.TryParse(match.Groups[1].Value, NumberStyles.None, CultureInfo.InvariantCulture, out first) &&
                Int64.TryParse(match.Groups[2].Value, NumberStyles.None, CultureInfo.InvariantCulture, out last) &&
                Int64.TryParse(match.Groups[3].Value, NumberStyles.None, CultureInfo.InvariantCulture, out total),
                "Invalid Content-Range");
        first = Int64.Parse(match.Groups[1].Value, CultureInfo.InvariantCulture);
        last = Int64.Parse(match.Groups[2].Value, CultureInfo.InvariantCulture);
        total = Int64.Parse(match.Groups[3].Value, CultureInfo.InvariantCulture);
        Require(first == offset && last == expectedSize - 1 && total == expectedSize, "Mismatched Content-Range");
    }

    private static void ValidateResponse(HttpWebResponse response, long offset)
    {
        string encoding = response.ContentEncoding;
        Require(String.IsNullOrEmpty(encoding) ||
                String.Equals(encoding, "identity", StringComparison.OrdinalIgnoreCase), "Unexpected encoding");
        if (response.StatusCode == HttpStatusCode.OK)
        {
            Require(offset == 0, "Server ignored the resume range");
            Require(String.IsNullOrEmpty(response.Headers["Content-Range"]), "Unexpected range on 200 response");
        }
        else if (response.StatusCode == HttpStatusCode.PartialContent)
        {
            ValidateRangeHeader(response.Headers["Content-Range"], offset, ExpectedSize);
        }
        else throw new InvalidDownload("Unexpected HTTP status");
        Require(response.ContentLength == ExpectedSize - offset, "Mismatched Content-Length");
    }

    private static void Transfer(Uri uri, FileStream output)
    {
        for (int attempt = 0; attempt < Attempts && output.Length < ExpectedSize; attempt++)
        {
            CheckStop();
            long offset = output.Length;
            output.Position = offset;
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(uri);
            request.AllowAutoRedirect = false;
            request.AutomaticDecompression = DecompressionMethods.None;
            request.Headers[HttpRequestHeader.AcceptEncoding] = "identity";
            request.UserAgent = "CommunityAI-Online-Installer/1";
            request.Timeout = IoTimeout;
            request.ReadWriteTimeout = IoTimeout;
            request.KeepAlive = false;
            if (offset > 0) request.AddRange(offset);
            lock (RequestLock) { CurrentRequest = request; }
            bool retry = false;
            try
            {
                CheckStop();
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    ValidateResponse(response, offset);
                    using (Stream input = response.GetResponseStream())
                    {
                        byte[] buffer = new byte[1024 * 1024];
                        int count;
                        while ((count = input.Read(buffer, 0, buffer.Length)) != 0)
                        {
                            CheckStop();
                            Require(count <= ExpectedSize - output.Position, "Response exceeded expected size");
                            output.Write(buffer, 0, count);
                            Downloaded = output.Position;
                            Progress(0, false);
                        }
                    }
                }
                if (output.Length != ExpectedSize) throw new IOException("Incomplete response");
            }
            catch (WebException error)
            {
                CheckStop();
                HttpWebResponse failed = error.Response as HttpWebResponse;
                if (failed != null)
                {
                    using (failed)
                    {
                        int status = (int)failed.StatusCode;
                        Require(status == 408 || status == 429 || status == 500 || status == 502 ||
                                status == 503 || status == 504, "Non-retryable HTTP status");
                        LastTransferError = "HTTP " + status.ToString(CultureInfo.InvariantCulture);
                    }
                }
                else LastTransferError = "Network " + error.Status.ToString();
                retry = true;
            }
            catch (IOException) { CheckStop(); LastTransferError = "Interrupted response or file I/O"; retry = true; }
            finally
            {
                lock (RequestLock) { CurrentRequest = null; }
                request.Abort();
            }
            if (output.Length == ExpectedSize) break;
            Require(retry && attempt + 1 < Attempts, "Retry budget exhausted: " + LastTransferError);
            Progress(1, false);
            Cancelled.WaitOne(Math.Min(30000, BackoffBase * (1 << attempt)));
        }
        CheckStop();
        Require(output.Length == ExpectedSize, "Incomplete download");
    }

    private static int Run(string[] args)
    {
        Require(args.Length == 8, "Expected eight arguments");
        Uri uri = ValidateUrl(args[0]);
        OutputPath = Path.GetFullPath(args[1]);
        Require(Int64.TryParse(args[2], NumberStyles.None, CultureInfo.InvariantCulture, out ExpectedSize) &&
                ExpectedSize > 0 && ExpectedSize <= 32L * 1024 * 1024 * 1024, "Invalid size");
        Require(Regex.IsMatch(args[3], @"\A[0-9a-f]{64}\z"), "Invalid SHA-256");
        string progress = Path.GetFullPath(args[4]);
        string cancel = Path.GetFullPath(args[5]);
        string[] paths = { OutputPath, progress, cancel, progress + ".new", progress + ".error", progress + ".warning" };
        for (int i = 0; i < paths.Length; i++)
            for (int j = 0; j < i; j++)
                Require(!String.Equals(paths[i], paths[j], StringComparison.OrdinalIgnoreCase), "Paths must differ");
        ProgressPath = progress;
        CancelPath = cancel;
        int parentPid;
        long parentCreation;
        Require(Int32.TryParse(args[6], out parentPid) && parentPid > 0 && parentPid != Process.GetCurrentProcess().Id,
                "Invalid parent PID");
        Require(Int64.TryParse(args[7], NumberStyles.None, CultureInfo.InvariantCulture, out parentCreation) &&
                parentCreation > 0, "Invalid parent creation time");
        Parent = Process.GetProcessById(parentPid);
        ParentHandle = Parent.Handle; // Retain the actual process handle through all I/O and cleanup.
        Require(Parent.StartTime.ToFileTimeUtc() == parentCreation &&
                WaitForSingleObject(ParentHandle, 0) == 258, "Parent identity mismatch");
        if (File.Exists(CancelPath)) { StopReason = 1; throw new Stopped(); }
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        Clock.Start();
        Thread watcher = new Thread(Watch);
        watcher.IsBackground = true;
        watcher.Start();
        try
        {
            using (FileStream output = new FileStream(OutputPath, FileMode.CreateNew,
                                                      FileAccess.ReadWrite, FileShare.Read))
            {
                OwnsOutput = true;
                Progress(0, true);
                Transfer(uri, output);
                output.Flush();
                output.Position = 0;
                Progress(2, true);
                using (SHA256 hash = SHA256.Create())
                {
                    byte[] buffer = new byte[1024 * 1024];
                    int count;
                    while ((count = output.Read(buffer, 0, buffer.Length)) != 0)
                    {
                        CheckStop();
                        hash.TransformBlock(buffer, 0, count, buffer, 0);
                    }
                    hash.TransformFinalBlock(new byte[0], 0, 0);
                    string digest = BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant();
                    Require(digest == args[3] && output.Length == ExpectedSize, "SHA-256 or size mismatch");
                }
                CheckStop();
                Progress(3, true);
                CheckStop();
            }
            return 0;
        }
        finally
        {
            Finished.Set();
            watcher.Join();
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        int result = 1;
        try { result = Run(args); }
        catch (Stopped)
        {
            result = StopReason == 3 ? 2 : 3;
            FailureDetail = StopReason == 3 ? "Two-hour download deadline exceeded" :
                            (StopReason == 2 ? "The parent installer exited" : "Download cancelled");
        }
        catch (Exception error)
        {
            result = 1;
            FailureDetail = error is InvalidDownload ? error.Message :
                            "Download failed (" + error.GetType().Name + ")";
        }
        finally
        {
            Finished.Set();
            if (result != 0)
            {
                if (OwnsOutput) { try { File.Delete(OutputPath); } catch { } }
                if (ProgressPath != null)
                {
                    try { Progress(result == 3 ? 5 : 4, true); } catch { }
                    try
                    {
                        string detail = FailureDetail.Replace('\r', ' ').Replace('\n', ' ');
                        if (detail.Length > 1024) detail = detail.Substring(0, 1024);
                        File.WriteAllText(ProgressPath + ".error", detail, new UTF8Encoding(false));
                    }
                    catch { }
                }
            }
            if (Parent != null) { try { Parent.Dispose(); } catch { } }
        }
        return result;
    }
}
