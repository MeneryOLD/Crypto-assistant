import "dotenv/config";
import { Bot, GrammyError, HttpError } from "grammy";
import { PrismaClient } from "@prisma/client";

const token = process.env.BOT_TOKEN;

if (!token) {
  throw new Error("BOT_TOKEN is required");
}

const prisma = new PrismaClient();
const bot = new Bot(token);

bot.command("start", async (ctx) => {
  const telegramUser = ctx.from;

  if (!telegramUser) {
    await ctx.reply("I couldn't identify your Telegram account.");
    return;
  }

  await prisma.user.upsert({
    where: { telegramId: BigInt(telegramUser.id) },
    update: {
      username: telegramUser.username,
      firstName: telegramUser.first_name,
      lastName: telegramUser.last_name
    },
    create: {
      telegramId: BigInt(telegramUser.id),
      username: telegramUser.username,
      firstName: telegramUser.first_name,
      lastName: telegramUser.last_name
    }
  });

  await ctx.reply(`Hello, ${telegramUser.first_name}! Your profile has been saved.`);
});

bot.catch((error) => {
  const { ctx } = error;
  console.error(`Error while handling update ${ctx.update.update_id}:`);

  const cause = error.error;
  if (cause instanceof GrammyError) {
    console.error("Telegram API error:", cause.description);
  } else if (cause instanceof HttpError) {
    console.error("Telegram network error:", cause);
  } else {
    console.error("Unexpected bot error:", cause);
  }
});

async function shutdown(signal: string): Promise<void> {
  console.log(`Received ${signal}; shutting down.`);
  bot.stop();
  await prisma.$disconnect();
}

process.once("SIGINT", () => void shutdown("SIGINT"));
process.once("SIGTERM", () => void shutdown("SIGTERM"));

bot.start({
  onStart: (botInfo) => {
    console.log(`Bot @${botInfo.username} is running.`);
  }
}).catch(async (error: unknown) => {
  console.error("Failed to start bot:", error);
  await prisma.$disconnect();
  process.exitCode = 1;
});
