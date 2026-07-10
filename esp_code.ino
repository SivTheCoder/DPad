const int BTN_CLICK = 2;
const int BTN_UP = 3;
const int BTN_DOWN = 4;

bool lastClick = HIGH;
bool lastUp = HIGH;
bool lastDown = HIGH;

void setup() {

  Serial.begin(115200);

  pinMode(BTN_CLICK, INPUT_PULLUP);
  pinMode(BTN_UP, INPUT_PULLUP);
  pinMode(BTN_DOWN, INPUT_PULLUP);

}

void loop() {

  bool click = digitalRead(BTN_CLICK);
  bool up = digitalRead(BTN_UP);
  bool down = digitalRead(BTN_DOWN);

  if (lastClick == HIGH && click == LOW) {

    Serial.println("ACTION_CLICK");

  }

  if (lastUp == HIGH && up == LOW) {

    Serial.println("ACTION_SCROLL_UP");

  }

  if (lastDown == HIGH && down == LOW) {

    Serial.println("ACTION_SCROLL_DOWN");

  }

  lastClick = click;
  lastUp = up;
  lastDown = down;

  delay(10);

}
