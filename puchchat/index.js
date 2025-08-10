// index.js
import dotenv from 'dotenv';
import { createClient } from '@supabase/supabase-js';
import WebSocket from 'ws'; // This needs to be installed: npm install ws
import { fileURLToPath } from 'url';
import path from 'path';
import pino from 'pino';

// --- Setup Logging ---
const logger = pino({
    level: process.env.LOG_LEVEL || 'info', // Default to 'info', set LOG_LEVEL=debug for more verbose output
    transport: {
        target: 'pino-pretty',
        options: {
            colorize: true,
            translateTime: 'SYS:yyyy-mm-dd HH:MM:ss',
            ignore: 'pid,hostname',
        },
    },
});

// Load environment variables
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
dotenv.config({ path: path.resolve(__dirname, '../.env') });

const TOKEN = process.env.AUTH_TOKEN;
const SUPABASE_KEY = process.env.SUPABASE_KEY;
const SUPABASE_URL = process.env.SUPABASE_URL;
logger.info("Attempting to load environment variables...");

if (!TOKEN) {
    logger.error("AUTH_TOKEN environment variable not set. Exiting.");
    process.exit(1);
}
if (!SUPABASE_URL) {
    logger.error("SUPABASE_URL environment variable not set. Exiting.");
    process.exit(1);
}
if (!SUPABASE_KEY) {
    logger.error("SUPABASE_KEY environment variable not set. Exiting.");
    process.exit(1);
}

logger.info("Environment variables loaded successfully.");
logger.debug(`SUPABASE_URL: ${SUPABASE_URL?.substring(0, 30)}...`);
logger.debug(`SUPABASE_KEY: ${SUPABASE_KEY?.substring(0, 5)}...`);

// Initialize Supabase client
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY, {
    auth: {
        persistSession: false,
    }
});
logger.info("Supabase client initialized.");

// --- Session State (per process, not per user) ---
const session = {
    username: null,
    last_message_id: 0
};
logger.info("Global session state initialized.");

// --- Supabase Realtime Listener ---
let realtimeChannel;

const startRealtimeListener = () => {
    logger.info("Starting Supabase Realtime listener...");
    try {
        realtimeChannel = supabase.channel('puchchat_messages', {
            config: {
                presence: {
                    key: 'puchchat_user_presence'
                }
            }
        });

        realtimeChannel
            .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'puchchat' }, (payload) => {
                logger.info("Received WS message from realtime.");
                const row = payload.new;
                const msg = row.message;
                const username = row.username;
                const sent_at = row.created_at;
                logger.info(`[Realtime] ${new Date(sent_at).toLocaleString()} ${username}: ${msg}`);
            })
            .on('postgres_changes', { event: '*', schema: 'public', table: 'puchchat_users' }, (payload) => {
                // Handle user presence changes if needed, e.g., for `connected_users` to update dynamically
                logger.info(`[Realtime] Supabase Realtime: Received user change event: ${payload.eventType} for user ${payload.new?.username || payload.old?.username}.`);
                // You might want to trigger a refresh of connected users or emit an event here
            })
            .subscribe((status) => {
                if (status === 'SUBSCRIBED') {
                    logger.info("Supabase Realtime: Subscribed to puchchat_messages channel.");
                } else if (status === 'CHANNEL_ERROR') {
                    logger.error("Supabase Realtime: Channel error.");
                } else if (status === 'TIMED_OUT') {
                    logger.warn("Supabase Realtime: Subscription timed out.");
                } else if (status === 'CLOSED') {
                    logger.info("Supabase Realtime: Channel closed. Attempting to re-subscribe.");
                    setTimeout(() => startRealtimeListener(), 5000); // Reconnect
                }
            });

        // Handle initial presence state (optional, for connected_users)
        realtimeChannel.subscribe(async (status) => {
            if (status === 'SUBSCRIBED') {
                const presence = realtimeChannel.track({ user: session.username || 'anonymous', status: 'online' });
                logger.info("Supabase Realtime: Presence tracked.");
            }
        });

    } catch (e) {
        logger.error(`Failed to start Realtime listener: ${e.message}`, { error: e });
        setTimeout(() => startRealtimeListener(), 5000); // Retry after 5 seconds
    }
};

// Start the realtime listener
startRealtimeListener();


// --- MCP Server (Conceptual - Not directly translatable to a simple Node.js file) ---
// The original Python code uses 'FastMCP' which is a specific framework for
// creating "Micro-Controller Protocols" or "Micro-Cloud Protocols".
// Directly converting FastMCP to Node.js is not straightforward as there isn't
// a direct equivalent library.
//
// For this conversion, I'll focus on the core logic of the tools
// (connect, disconnect, send, fetch, help, connected_users)
// and how they interact with Supabase.
//
// If you need a full MCP server equivalent in Node.js, you would typically
// use a web framework like Express.js or Fastify, define API endpoints,
// and implement authentication similar to how FastMCP handles it.
//
// For simplicity, I'll expose these as asynchronous functions that could be
// called by a hypothetical command-line interface or a web API.

// A simple authentication placeholder - in a real app, this would be more robust
class SimpleAuthProvider {
    constructor(token) {
        this.token = token;
        logger.info("SimpleAuthProvider initialized.");
    }

    async verify(providedToken) {
        logger.debug(`Attempting to verify token: ${providedToken?.substring(0, 10)}...`);
        if (providedToken === this.token) {
            logger.info("Token matched. Authentication successful.");
            return { clientId: "puch-client", scopes: ["*"] };
        }
        logger.warn("Token mismatch. Authentication failed.");
        return null;
    }
}
const authProvider = new SimpleAuthProvider(TOKEN);

// --- Tools ---

async function connect(username, authToken) {
    logger.info(`Tool 'connect' called for username: ${username}`);
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    try {
        username = username.trim().toLowerCase();
        session.username = username;

        const { data: userData, error: userError } = await supabase
            .from("puchchat_users")
            .select("*")
            .eq("username", username);

        if (userError && userError.code !== 'PGRST116') { // PGRST116 is 'No rows found'
            logger.error(`Error fetching user '${username}': ${userError.message}`, { error: userError });
            return `❌ Error fetching user data for '${username}': ${userError.message}`;
        }

        if (userData && userData.length > 0) {
            const { error: updateError } = await supabase
                .from("puchchat_users")
                .update({ is_connected: true })
                .eq("username", username);
            if (updateError) {
                logger.error(`Error updating user '${username}': ${updateError.message}`, { error: updateError });
                return `❌ Error updating connection status for '${username}': ${updateError.message}`;
            }
            logger.info(`User '${username}' updated as connected.`);
        } else {
            const { error: insertError } = await supabase
                .from("puchchat_users")
                .insert({ username: username, is_connected: true });
            if (insertError) {
                logger.error(`Error inserting new user '${username}': ${insertError.message}`, { error: insertError });
                return `❌ Error connecting as new user '${username}': ${insertError.message}`;
            }
            logger.info(`New user '${username}' inserted and connected.`);
        }

        session.last_message_id = 0;
        return `✅ Connected as '${username}'.`;
    } catch (e) {
        logger.error(`Error in 'connect' tool for username '${username}': ${e.message}`, { error: e });
        return `❌ An unexpected error occurred while connecting: ${e.message}`;
    }
}

async function disconnect(authToken) {
    logger.info("Tool 'disconnect' called.");
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    const username = session.username;
    if (!username) {
        logger.warn("Disconnect called but no user in session.");
        return "⚠️ You are not connected.";
    }
    try {
        const { error } = await supabase
            .from("puchchat_users")
            .update({ is_connected: false })
            .eq("username", username);
        if (error) {
            logger.error(`Error disconnecting user '${username}': ${error.message}`, { error });
            return `❌ Error disconnecting '${username}': ${error.message}`;
        }

        logger.info(`User '${username}' updated as disconnected.`);
        session.username = null;
        session.last_message_id = 0;
        return `🚪 User '${username}' disconnected from chat.`;
    } catch (e) {
        logger.error(`Error in 'disconnect' tool for username '${username}': ${e.message}`, { error: e });
        return `❌ An unexpected error occurred while disconnecting: ${e.message}`;
    }
}

async function send(message, authToken) {
    logger.info(`Tool 'send' called with message: ${message?.substring(0, 50)}...`);
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    const username = session.username;
    if (!username) {
        logger.warn("Send called but no user in session. Prompting connect.");
        return "⚠️ You must /connect before sending messages.";
    }
    try {
        const { data: userData, error: userError } = await supabase
            .from("puchchat_users")
            .select("is_connected")
            .eq("username", username)
            .single();

        if (userError || !userData || !userData.is_connected) {
            logger.warn(`User '${username}' tried to send but is not marked as connected in DB.`, { userError, userData });
            return "⚠️ You are not connected. Use /connect first.";
        }

        const { error: insertError } = await supabase
            .from("puchchat")
            .insert({
                username: username,
                message: message.trim(),
            });

        if (insertError) {
            logger.error(`Error inserting message for '${username}': ${insertError.message}`, { error: insertError });
            return `❌ Error sending message: ${insertError.message}`;
        }

        logger.info(`Message sent and inserted into DB by '${username}'.`);
        return `📨 Message sent: ${message.trim()}`;
    } catch (e) {
        logger.error(`Error in 'send' tool for username '${username}' and message '${message?.substring(0, 50)}': ${e.message}`, { error: e });
        return `❌ An unexpected error occurred while sending your message: ${e.message}`;
    }
}

async function fetch(authToken) {
    logger.info("Tool 'fetch' called.");
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    const username = session.username;
    if (!username) {
        logger.warn("Fetch called but no user in session. Prompting connect.");
        return "⚠️ You must /connect before fetching messages.";
    }
    try {
        const { data: userData, error: userError } = await supabase
            .from("puchchat_users")
            .select("is_connected")
            .eq("username", username)
            .single();

        if (userError || !userData || !userData.is_connected) {
            logger.warn(`User '${username}' tried to fetch but is not marked as connected in DB.`, { userError, userData });
            return "⚠️ You are not connected. Use /connect first.";
        }

        const lastId = session.last_message_id || 0;
        logger.debug(`Fetching messages greater than ID: ${lastId}`);

        const { data: messages, error } = await supabase
            .from("puchchat")
            .select("*")
            .gt("id", lastId)
            .order("id", { ascending: true });

        if (error) {
            logger.error(`Error fetching messages for '${username}': ${error.message}`, { error });
            return `❌ Error fetching messages: ${error.message}`;
        }

        if (!messages || messages.length === 0) {
            logger.info("No new messages found.");
            return "🕒 No new messages.";
        }

        session.last_message_id = messages[messages.length - 1].id;
        logger.info(`Fetched ${messages.length} new messages. Updated last_message_id to ${session.last_message_id}`);

        const formatted = messages.map(m => {
            const date = new Date(m.created_at);
            const formattedDate = date.toISOString().replace('T', ' ').split('.')[0];
            return `[${formattedDate}] ${m.username}: ${m.message}`;
        }).join("\n");
        return `💬 New messages:\n${formatted}`;
    } catch (e) {
        logger.error(`Error in 'fetch' tool for username '${username}': ${e.message}`, { error: e });
        return `❌ An unexpected error occurred while fetching messages: ${e.message}`;
    }
}

async function help(authToken) {
    logger.info("Tool 'help' called.");
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    return (
        "Commands:\n" +
        "/connect <username> - Connect to chat\n" +
        "/disconnect - Disconnect from chat\n" +
        "/send <message> - Send a message (only when connected)\n" +
        "/fetch - Fetch new messages\n" +
        "/help - Show this help\n" +
        "/connected_users - Show who is connected\n"
    );
}

async function connectedUsers(authToken) {
    logger.info("Tool 'connected_users' called.");
    const isAuthenticated = await authProvider.verify(authToken);
    if (!isAuthenticated) {
        return "❌ Authentication failed.";
    }

    try {
        const { data: usersData, error } = await supabase
            .from("puchchat_users")
            .select("username")
            .eq("is_connected", true);

        if (error) {
            logger.error(`Error fetching connected users: ${error.message}`, { error });
            return `❌ Error fetching connected users: ${error.message}`;
        }

        const users = usersData ? usersData.map(u => u.username) : [];
        const count = users.length;
        if (count === 0) {
            logger.info("No users currently connected.");
            return "No users are currently connected.";
        }

        logger.info(`Found ${count} connected users.`);
        return `Connected users (${count}):\n` + users.join("\n");
    } catch (e) {
        logger.error(`Error in 'connected_users' tool: ${e.message}`, { error: e });
        return `❌ An unexpected error occurred while fetching connected users: ${e.message}`;
    }
}

// --- Example of how to use these functions (simulating MCP calls) ---
// This part demonstrates how you might call these functions. In a real
// application using a web framework (like Express.js), these would be
// associated with API routes.

const simulateCommand = async (command, args = [], token = TOKEN) => {
    logger.info(`Simulating command: ${command} with args: ${args}`);
    switch (command) {
        case 'connect':
            console.log(await connect(args[0], token));
            break;
        case 'disconnect':
            console.log(await disconnect(token));
            break;
        case 'send':
            console.log(await send(args[0], token));
            break;
        case 'fetch':
            console.log(await fetch(token));
            break;
        case 'help':
            console.log(await help(token));
            break;
        case 'connected_users':
            console.log(await connectedUsers(token));
            break;
        default:
            console.log(`Unknown command: ${command}`);
    }
};

// To run an example:
// simulateCommand('connect', ['testuser123']);
// setTimeout(() => simulateCommand('send', ['Hello from Node.js!']), 2000);
// setTimeout(() => simulateCommand('fetch'), 4000);
// setTimeout(() => simulateCommand('connected_users'), 6000);
// setTimeout(() => simulateCommand('disconnect'), 8000);


// A simple CLI to interact with the tools
process.stdin.resume();
process.stdin.setEncoding('utf8');

console.log("\nNode.js Supabase Chat CLI (type /help for commands):");
process.stdin.on('data', async (text) => {
    const input = text.trim();
    if (input.startsWith('/')) {
        const parts = input.substring(1).split(' ');
        const command = parts[0];
        const args = parts.slice(1).join(' '); // Join remaining parts for messages

        switch (command) {
            case 'connect':
                await simulateCommand('connect', [args]);
                break;
            case 'disconnect':
                await simulateCommand('disconnect');
                break;
            case 'send':
                await simulateCommand('send', [args]);
                break;
            case 'fetch':
                await simulateCommand('fetch');
                break;
            case 'help':
                await simulateCommand('help');
                break;
            case 'connected_users':
                await simulateCommand('connected_users');
                break;
            case 'exit':
                console.log('Exiting CLI.');
                process.exit();
            default:
                console.log(`Unknown command: /${command}. Type /help for a list of commands.`);
        }
    } else {
        // If not a command, treat it as a message to send if connected
        if (session.username) {
            await simulateCommand('send', [input]);
        } else {
            console.log("You are not connected. Use /connect <username> to join the chat.");
        }
    }
});

process.on('SIGINT', () => {
    logger.info('Received SIGINT. Shutting down.');
    if (realtimeChannel) {
        realtimeChannel.unsubscribe();
        logger.info('Unsubscribed from Supabase Realtime channel.');
    }
    process.exit();
});

logger.info("🚀 Node.js Supabase Chat Application started.");
logger.info("Type commands in the console (e.g., /connect myusername).");