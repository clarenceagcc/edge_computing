import torch
from torchvision import transforms
import torchaudio
import torchaudio.transforms as T
import torch.nn as nn
import numpy as np
import sounddevice as sd
import tkinter as tk
from threading import Thread, Event
import cv2
from PIL import Image, ImageTk
import os
import insightface
from insightface.app import FaceAnalysis
from scipy.spatial.distance import cosine
from door_lock import DoorLock
import RPi.GPIO as GPIO

RELAY_PIN = 21

sd.default.device=0
wake_word_detected=False
audio_stream = None

# --- Tkinter GUI Setup ---
root = tk.Tk()
root.title("Wake Word Detection with Camera Feed")
#audio_paused = Event()  # Event to control audio thread pause

# --- InsightFace Initialization ---
app = FaceAnalysis(name="buffalo_l", providers=['CPUExecutionProvider'])
app.prepare(ctx_id=0, det_size=(640, 640))
print("Buffalo I model loaded successfully!")

# Camera settings
webcam_resolution = (640, 480)
cap = None

# Create directory for saved faces
if not os.path.exists("saved_faces"):
    os.makedirs("saved_faces")

# Label to display the camera feed
camera_label = tk.Label(root)
camera_label.pack()

# --- Start the camera and display feed ---
def start_camera():
    global cap
    if cap is None or not cap.isOpened():
        #cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, webcam_resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, webcam_resolution[1])
        update_camera_feed()

def update_camera_feed():
    global cap
    if cap is not None and cap.isOpened(): #check if cap is valid.
        ret, frame = cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            imgtk = ImageTk.PhotoImage(image=img)
            camera_label.imgtk = imgtk
            camera_label.configure(image=imgtk)
        root.after(10, update_camera_feed)

# --- Face Registration ---
def open_face_camera():
    global cap
    #cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, webcam_resolution[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, webcam_resolution[1])


def register_face():
    global cap, face_window, wake_word_detected
    print(wake_word_detected)
    if not wake_word_detected:
        stop_audio_stream()  # Pause audio thread
        
    if cap is not None and cap.isOpened():
        cap.release()
        cv2.destroyAllWindows()
        cap = None  

    root.withdraw()  # Hide the main window

    face_window = tk.Toplevel(root)
    face_window.title("Face Registration")

    face_label = tk.Label(face_window)
    face_label.pack()

    save_button = tk.Button(face_window, text="Save Face", command=save_face)
    save_button.pack(pady=10)

    close_button = tk.Button(face_window, text="Close", command=close_face_registration)
    close_button.pack(pady=10)

    # Run camera feed in a separate thread
    thread = Thread(target=open_face_camera)
    thread.daemon = True
    thread.start()

    update_face_feed(face_label)

def update_face_feed(face_label):
    if face_window.winfo_exists():  # Ensure window still exists
        if cap is not None and cap.isOpened():
            ret, frame = cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame)
                imgtk = ImageTk.PhotoImage(image=img)
                face_label.imgtk = imgtk
                face_label.configure(image=imgtk)

        if face_window.winfo_exists():  # Double-check before scheduling the next update
            face_window.after(10, lambda: update_face_feed(face_label))


def close_face_registration():
    global cap, face_window, wake_word_detected
    print(wake_word_detected)
    if face_window.winfo_exists():
        face_window.destroy()

    if cap is not None:
        cap.release()
        cv2.destroyAllWindows()
        cap = None  # Avoid using a released camera
    
    if not wake_word_detected:
        start_audio_stream()  # Resume audio detection
    else:
        start_camera()
        
    root.deiconify()  # Show the main window again

def save_face():
    global cap, face_window, wake_word_detected
    print(wake_word_detected)
    if cap is not None and cap.isOpened():
        ret, frame = cap.read()
        if ret:
            face_filename = f"saved_faces/face_1.png"
            cv2.imwrite(face_filename, frame)
            print(f"Face saved as {face_filename}")
            cap.release()

    # Ensure the face_window is properly closed
    if face_window:
        face_window.destroy()  # Close the registration window
    
    if not wake_word_detected:
        start_audio_stream()  # Resume audio detection
    else:
        start_camera()
        
    root.deiconify()  # Show the main window again

# --- Display Saved Faces ---
def display_saved_faces():
    face_window = tk.Toplevel(root)
    face_window.title("Saved Faces")
    for i, filename in enumerate(os.listdir("saved_faces")):
        face_path = os.path.join("saved_faces", filename)
        img = Image.open(face_path)
        img = img.resize((100, 100))
        imgtk = ImageTk.PhotoImage(img)
        label = tk.Label(face_window, image=imgtk)
        label.image = imgtk
        label.grid(row=i // 5, column=i % 5, padx=5, pady=5)

# --- Wake Word Model ---
class WakeWordModel(nn.Module):
    def __init__(self):
        super(WakeWordModel, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, stride=1, padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, stride=1, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1)
        self.bn3 = nn.BatchNorm2d(128)
        self.pool = nn.MaxPool2d(2, 2)
        self.adaptive_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc1 = nn.Linear(128, 128)
        self.dropout = nn.Dropout(0.5)  # Dropout to reduce overfitting
        self.fc2 = nn.Linear(128, 1)
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        x = self.pool(self.relu(self.bn1(self.conv1(x))))
        x = self.pool(self.relu(self.bn2(self.conv2(x))))
        x = self.pool(self.relu(self.bn3(self.conv3(x))))
        x = self.adaptive_pool(x)
        x = x.view(x.size(0), -1)
        x = self.dropout(self.relu(self.fc1(x)))
        x = self.fc2(x)
        return x

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
wake_word_model = WakeWordModel().to(device)
wake_word_model.load_state_dict(torch.load("476998.pth", map_location=device))
wake_word_model.eval()

# --- Audio Configuration ---
SAMPLE_RATE = 16000
DURATION = 1.0
BUFFER_SIZE = int(SAMPLE_RATE * DURATION)
THRESHOLD = 0.78

# Transform for mel spectrogram
transform = T.MelSpectrogram(sample_rate=SAMPLE_RATE, n_mels=64, n_fft=400, hop_length=160).to(device)

# Initialize an empty buffer
audio_buffer = np.zeros(BUFFER_SIZE, dtype=np.float32)

# --- Audio Processing ---
def process_audio(audio_data):
    waveform = torch.tensor(audio_data, dtype=torch.float32).to(device)
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)  # Ensure batch dimension
    mel_spec = transform(waveform).unsqueeze(0)  # Add channel dimension for Conv2D
    return mel_spec

def detect_wake_word(audio_data):
    mel_spec = process_audio(audio_data)
    with torch.no_grad():
        output = wake_word_model(mel_spec)
        return torch.sigmoid(output).item()  # Sigmoid for probability
        
# --- Facial Detection ---
def recognize_face(frame):
    saved_face_img = cv2.imread("saved_faces/face_1.png")
    recognized = False  # Initialize the recognition flag
    
    if saved_face_img is None:
        print("Could not load saved face.")
        return frame, recognized

    saved_face = app.get(saved_face_img)
    if not saved_face:
        print("No face found in saved image.")
        return frame, recognized

    saved_embedding = saved_face[0]['embedding']

    faces = app.get(frame)
    if faces:
        for face in faces:
            embedding = face['embedding']
            dist = cosine(embedding, saved_embedding)
            if dist < 0.3: #threshold, modify as needed.
                print("Face Recognized!")
                recognized = True # Set flag when match found
                bbox = face['bbox'].astype(int)
                cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                break
            else:
                print("Face not recognized.")
    return frame, recognized
    
# --- Facial Detection and Recognition ---
def perform_face_recognition():
    global cap, wake_word_detected
    if cap is not None and cap.isOpened():
        ret, frame = cap.read()
        if ret:
            frame, recognized = recognize_face(frame)
            if recognized:
                lock_system.unlock()
                root.after(20000, lock_system.lock) #Lock after 10 seconds
            # Show the result in a new window
            face_result_window = tk.Toplevel(root)
            face_result_window.title("Face Recognition Result")
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            imgtk = ImageTk.PhotoImage(image=img)
            result_label = tk.Label(face_result_window, image=imgtk)
            result_label.imgtk = imgtk
            result_label.configure(image=imgtk)
            result_label.pack()
            
    wake_word_detected = False  # Reset wake word flag


# --- Audio Callback ---
def callback(indata, frames, time, status):
    global audio_buffer, wake_word_detected
    if status:
        print(f"Audio callback error: {status}")

    audio_buffer[:-frames] = audio_buffer[frames:]
    audio_buffer[-frames:] = indata[:, 0]
    
    prediction = detect_wake_word(audio_buffer)
    print(f"Wake word probability: {prediction}")

    if prediction > THRESHOLD:
        wake_word_detected = True
        print("Wake word detected! Scanning face...")
        stop_audio_stream()
        root.after(0, start_camera)
        print("Scanning face...")
        root.after(3000, perform_face_recognition)  # 3 second delay.
        
# --- audio threading --- #

def start_audio_stream():
    global audio_stream
    try:
        audio_stream = sd.InputStream(callback=callback, channels=1, samplerate=SAMPLE_RATE, blocksize=BUFFER_SIZE)
        audio_stream.start()
        print("Listening for wake word...")
    except Exception as e:
        print(f"Error starting audio stream: {e}")

def stop_audio_stream():
    global audio_stream
    if audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None  # Clear the stream object
        print("Audio stream stopped.")
        
# --- Add Buttons ---
button_frame = tk.Frame(root)
button_frame.pack()

register_button = tk.Button(button_frame, text="Register Face", command=register_face)
register_button.pack(side=tk.LEFT, padx=10, pady=10)

display_button = tk.Button(button_frame, text="Display Saved Faces", command=display_saved_faces)
display_button.pack(side=tk.LEFT, padx=10, pady=10)

# --- Start Application ---
if __name__ == "__main__":
    lock_system = DoorLock()
    root.geometry("640x580")
    label = tk.Label(root, text="Waiting for wake word...")
    label.pack(pady=10)

    Thread(target=start_audio_stream, daemon=True).start()
    
    try:
        root.mainloop()
    finally:
        if cap is not None and cap.isOpened():
            cap.release()
        lock_system.cleanup()
